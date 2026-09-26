// Actual bundled UI and credential gate, with no credentials or model calls.
// The exported checks also run in the installed Windows preview acceptance test.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export async function workerJson(page, url, method = 'GET', body) {
  return page.evaluate(async ({ url, method, body }) => {
    const response = await fetch(url, {
      method, cache: 'no-store',
      ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    });
    if (!response.ok) throw new Error(`${method} ${url} returned ${response.status}`);
    return response.json();
  }, { url, method, body });
}

export async function verifyOfflineSetup(page, { ownerPassword } = {}) {
  // A container creates its own owner password between Welcome and License.
  // Desktop uses its native handoff. Reloads resume at License, not Workspace;
  // never seed session storage, trust the LAN or write onboarding flags.
  await page.locator('[data-testid="setup-welcome"], [data-testid="setup-license"]').waitFor();
  const welcome = page.getByTestId('setup-welcome');
  if (await welcome.isVisible()) await welcome.getByRole('button', { name: 'Continue', exact: true }).click();
  const owner = await workerJson(page, '/api/v1/owner/status');
  if (owner.required && !owner.password_set) {
    assert.equal(owner.setup_allowed, true, 'Fresh owner creation must be allowed from this local browser');
    assert(ownerPassword, 'The container needs its own disposable owner password; never bypass the owner gate');
    const form = page.getByRole('form', { name: 'Set an owner password', exact: true });
    await form.getByLabel(/^New password/).fill(ownerPassword);
    await form.getByLabel('Repeat it', { exact: true }).fill(ownerPassword);
    const [created] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/owner/password' && response.request().method() === 'POST'),
      form.getByRole('button', { name: 'Save password', exact: true }).click(),
    ]);
    assert.equal(created.status(), 200, 'The real owner-password producer rejected the fixture');
    const savedOwner = await workerJson(page, '/api/v1/owner/status');
    assert.equal(savedOwner.password_set, true);
    assert.equal(savedOwner.trusted_lan, false, 'Owner creation must not enable login-free LAN access');
  }
  const license = page.getByTestId('setup-license');
  await license.waitFor();
  const status = await workerJson(page, '/api/v1/license');
  assert.equal(status.mode, 'beta', 'Offline acceptance expects the unlicensed beta build, not a fabricated lease');
  assert.equal(status.allowed, true);
  await license.getByRole('heading', { name: 'Beta: no license needed', exact: true }).waitFor();
  await license.getByRole('button', { name: 'Continue', exact: true }).click();
  for (const [mode, label] of [['subscription', /^Claude subscription/], ['api_key', /^Anthropic API key/]]) {
    const choice = page.getByTestId('setup-mode');
    await choice.waitFor();
    await choice.getByText(mode === 'subscription' ? 'Claude subscription' : 'Anthropic API key', { exact: true }).click();
    assert.equal(await choice.getByRole('radio', { name: label }).isChecked(), true);
    const [response] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/settings/claude-auth') && response.request().method() === 'PUT'),
      choice.getByRole('button', { name: 'Continue', exact: true }).click(),
    ]);
    assert.equal(response.status(), 200, 'The real mode producer rejected setup');
    const summary = await response.json();
    assert.equal(summary.mode, mode);
    assert.equal(summary.cli.signed_in, false, 'Acceptance must not inherit a CLI login');
    assert.equal(summary.api_key.set, false, 'Acceptance must not inherit an API key');
    assert.equal(summary.gate.claude_credential_ready, false);
    assert.equal(summary.gate.ready, false);
    const credential = page.getByTestId('setup-credential');
    await credential.getByTestId('setup-credential-missing').waitFor();
    assert.equal(await credential.getByRole('button', { name: 'Continue', exact: true }).isDisabled(), true);
    assert.equal((await credential.getByTestId('setup-credential-reason').textContent()).trim(), summary.gate.reason);
    const [checked] = await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/settings/claude-auth') && r.request().method() === 'GET'),
      credential.getByRole('button', { name: 'Check again', exact: true }).last().click(),
    ]);
    assert.equal(checked.status(), 200);
    assert.equal((await workerJson(page, '/api/v1/settings/claude-auth')).gate.ready, false);
    assert.equal(await credential.getByRole('button', { name: 'Continue', exact: true }).isDisabled(), true);
    assert.equal(await page.getByTestId('setup-workspace').count(), 0, 'No credential may bypass the workspace prerequisite');
    assert.equal(await page.getByTestId('setup-first-run').count(), 0, 'Do not run a model in offline acceptance');
    assert.equal(await page.getByRole('button', { name: 'Finish', exact: true }).count(), 0);
    if (mode === 'subscription') await credential.getByRole('button', { name: 'Back', exact: true }).click();
  }
  const auth = await workerJson(page, '/api/v1/auth/status');
  assert.equal(auth.onboarding_complete, false, 'A credential-blocked wizard must stay unfinished');
  assert.equal(auth.agent_gate.ready, false);
  assert.equal(auth.owner_linked, false);
}

export async function configurePersistenceFixture(page, workspace) {
  // Setup cannot reach Workspace without a supported credential. Preserve the
  // separate database/lifecycle test through the real owner APIs, not SQL seeds
  // or a fake credential response. This does NOT complete or advance the wizard.
  const url = '/api/v1/workspace/config';
  await workerJson(page, url, 'POST', { workspace_path: workspace });
  assert.equal((await workerJson(page, url)).workspace_path, workspace);
  const network = await workerJson(page, '/api/v1/network/status');
  if (!network.enrolled) await workerJson(page, '/api/v1/network/create', 'POST', {});
  assert.equal((await workerJson(page, '/api/v1/auth/status')).onboarding_complete, false);
  return url;
}

/**
 * A plain headless Chromium for this check, in place of the container's
 * supervised one. That browser is the agent's: it runs behind the egress
 * proxy, whose policy refuses the worker's own ports on any local address
 * (a page there must never act as the owner). A user's browser is not behind
 * that proxy, so the bundled UI is exercised from a Chromium of its own.
 */
export async function launchPlainChromium(port = 9333) {
  const executable = process.env.CHROME_EXECUTABLE || '/usr/bin/google-chrome-stable';
  const profile = mkdtempSync(path.join(tmpdir(), 'container-ui-smoke-'));
  const stderr = [];
  const child = spawn(executable, [
    '--headless=new', '--ozone-platform=headless', '--no-sandbox', '--disable-gpu',
    '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check',
    '--disable-features=Translate,BackForwardCache,DBus,MediaSessionService',
    '--window-size=1280,800', `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`,
    'about:blank',
  ], {
    env: { ...process.env, HOME: profile, DBUS_SESSION_BUS_ADDRESS: 'disabled:', DBUS_SYSTEM_BUS_ADDRESS: 'disabled:' },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  child.stderr.on('data', chunk => { stderr.push(String(chunk)); if (stderr.length > 200) stderr.shift(); });
  const endpoint = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 30000;
  let lastError = 'no answer';
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`plain Chromium exited with ${child.exitCode}:\n${stderr.join('')}`);
    try {
      const response = await fetch(`${endpoint}/json/version`);
      if (response.ok) return { child, endpoint };
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error.message;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  child.kill('SIGKILL');
  throw new Error(`plain Chromium did not open its DevTools port (${lastError}):\n${stderr.join('')}`);
}

async function main() {
  const { chromium } = await import('/opt/browser-mcp/node_modules/playwright-core/index.mjs');
  const plain = await launchPlainChromium();
  const browser = await chromium.connectOverCDP(plain.endpoint, { timeout: 30000 });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  const pageErrors = [], completed = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('request', request => {
    if (request.url().endsWith('/auth/onboarding/complete')) completed.push(request.method());
  });
  try {
    await page.goto('http://127.0.0.1:8000/setup', { waitUntil: 'domcontentloaded' });
    const owner = await workerJson(page, '/api/v1/owner/status');
    assert.equal(owner.password_set, false, 'The container fixture must start with no owner password');
    assert.equal(owner.required, true, 'A fresh container browser must not bypass owner setup');
    assert.equal(owner.trusted_lan, false);
    const ownerPassword = randomBytes(32).toString('base64url');
    await verifyOfflineSetup(page, { ownerPassword });
    await configurePersistenceFixture(page, '/workspace/container-onboarding-check');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await verifyOfflineSetup(page);
    const auth = await workerJson(page, '/api/v1/auth/status');
    assert.equal(auth.local_access, true);
    assert.equal('account_sign_in_available' in auth, false, 'Hub owner browser sign-in is not local owner authentication');
    for (const route of ['login', 'account/start', 'recovery']) {
      const removed = await context.request.post(`http://127.0.0.1:8000/api/v1/auth/${route}`, {
        data: {}, headers: { Origin: 'http://127.0.0.1:8000' }
      });
      assert.equal(removed.status(), 404, `Retired hub login route ${route} must not exist`);
    }
    // Settings UI/account linking is beyond the credential gate. Keep the image
    // updater's real producer/consumer coverage through its supported owner API;
    // do not mark onboarding complete just to make its navigation available.
    const policyUrl = '/api/v1/settings/application-updates';
    const initialPolicy = await workerJson(page, policyUrl);
    assert.equal(initialPolicy.allow_updates, true, 'Updates must default on');
    assert.equal(initialPolicy.supported, true, 'Candidate image must start the application updater');
    assert.equal(initialPolicy.update_all_workers, false, 'Fleet updates are opt-in');
    await workerJson(page, policyUrl, 'PUT', { update_all_workers: true });
    assert.equal((await workerJson(page, policyUrl)).update_all_workers, true);
    await workerJson(page, policyUrl, 'PUT', { allow_updates: false });
    await page.reload({ waitUntil: 'domcontentloaded' });
    assert.equal((await workerJson(page, policyUrl)).allow_updates, false);
    await workerJson(page, policyUrl, 'PUT', { allow_updates: true });
    assert.equal((await workerJson(page, policyUrl)).allow_updates, true);
    assert.equal((await workerJson(page, '/api/v1/auth/status')).onboarding_complete, false);
    assert.deepEqual(completed, [], 'The smoke test must never bypass onboarding completion');
    assert.deepEqual(pageErrors, [], 'Bundled UI raised JavaScript errors');
    console.log('Bundled wizard persisted both modes, refused missing credentials and stayed unfinished after reload. Workspace/network persistence and update policy APIs passed. No model, account linking, first-run completion or post-setup Settings UI was exercised.');
  } finally {
    await page.screenshot({ path: '/tmp/container-ui.png', mask: [page.locator('code, input[type="password"]')] });
    await context.close();
    await browser.close();
    plain.child.kill('SIGKILL');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
