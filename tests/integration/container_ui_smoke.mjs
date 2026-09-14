// Actual bundled desktop UI, driven through the image's supervised Chromium.
import assert from 'node:assert/strict';
import { chromium } from '/opt/browser-mcp/node_modules/playwright-core/index.mjs';

const browser = await chromium.connectOverCDP('http://127.0.0.1:9221', { timeout: 30000 });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));
try {
  await page.goto('http://127.0.0.1:8000/setup', { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: 'Welcome to AI Shuffle', exact: true }).waitFor();
  // Start step: a clean install needs no account, invite or approval.
  await page.getByRole('button', { name: 'Continue without an account', exact: true }).click();
  await page.getByRole('heading', { name: 'Select Workspace Folder', exact: true }).waitFor();
  await page.getByLabel('Or type a path manually').fill('/workspace/container-onboarding-check');
  const saved = page.waitForResponse(response => response.url().endsWith('/workspace/config') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  const response = await saved;
  assert.equal(response.status(), 200, 'Workspace producer rejected the setup request');
  const config = await page.evaluate(async url => {
    const current = await fetch(url);
    if (!current.ok) throw new Error(`Workspace consumer returned ${current.status}`);
    return current.json();
  }, response.url());
  assert.equal(config.workspace_path, '/workspace/container-onboarding-check');
  // Providers & GitHub is the last step; no Claude Code login is offered.
  await page.getByText('Model providers', { exact: true }).waitFor();
  await page.getByText('GitHub', { exact: true }).waitFor();
  assert.equal(await page.getByText(/Claude Code/).count(), 0, 'Setup must not ask for Claude Code');
  const completed = page.waitForResponse(response => response.url().endsWith('/auth/onboarding/complete') && response.request().method() === 'POST');
  await page.getByRole('button', { name: /^Finish/ }).click();
  assert.equal((await completed).status(), 200, 'Standalone onboarding completion was rejected');
  await page.waitForURL(url => url.origin === 'http://127.0.0.1:8000' && url.pathname !== '/setup');
  const auth = await (await context.request.get('http://127.0.0.1:8000/api/v1/auth/status')).json();
  assert.equal(auth.local_access, true);
  assert.equal(auth.owner_linked, false);
  // Shuffle never asks its owner to sign in: status offers no browser sign-in and the login routes are gone.
  assert.equal('account_sign_in_available' in auth, false, 'Fresh worker must not offer remote owner browser sign-in');
  for (const route of ['login', 'account/start', 'recovery']) {
    const removed = await context.request.post(`http://127.0.0.1:8000/api/v1/auth/${route}`, {
      data: {}, headers: { Origin: 'http://127.0.0.1:8000' }
    });
    assert.equal(removed.status(), 404, `Owner login route ${route} must not exist`);
  }
  await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'More', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  const updates = page.getByRole('switch', { name: 'Allow updates', exact: true });
  await updates.waitFor();
  const policyUrl = 'http://127.0.0.1:8000/api/v1/settings/application-updates';
  const initialPolicy = await (await context.request.get(policyUrl)).json();
  assert.equal(initialPolicy.allow_updates, true, 'Updates must default on');
  assert.equal(initialPolicy.supported, true, 'Candidate image must start the application updater');
  assert.equal(initialPolicy.update_all_workers, false, 'Fleet updates are opt-in');
  const scopeChanged = page.waitForResponse(response => response.url() === policyUrl && response.request().method() === 'PUT');
  await page.getByRole('switch', { name: 'Update all workers', exact: true }).click();
  assert.equal((await scopeChanged).status(), 200);
  assert.equal((await (await context.request.get(policyUrl)).json()).update_all_workers, true);
  const changed = page.waitForResponse(response => response.url() === policyUrl && response.request().method() === 'PUT');
  await updates.click();
  assert.equal((await changed).status(), 200);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByText('Application updates are paused. Your current version will keep running.', { exact: true }).waitFor();
  assert.equal(await page.getByRole('switch', { name: 'Allow updates', exact: true }).isChecked(), false);
  const enabled = page.waitForResponse(response => response.url() === policyUrl && response.request().method() === 'PUT');
  await page.getByRole('switch', { name: 'Allow updates', exact: true }).click();
  assert.equal((await enabled).status(), 200);
  assert.equal((await (await context.request.get(policyUrl)).json()).allow_updates, true);
  await page.getByRole('button', { name: 'Providers', exact: true }).click();
  const account = page.getByRole('region', { name: 'Shuffle account', exact: true });
  const login = account.getByRole('button', { name: 'Sign in', exact: true });
  await login.waitFor();
  assert.equal(await login.isEnabled(), true, 'Fresh local owner must be able to link a Shuffle account for phone access');
  assert.equal(await account.getByRole('button', { name: 'Create account', exact: true }).count(), 0, 'Account creation belongs to setup; settings offers sign in');
  assert.equal(await page.getByText(/Pending approvals/).count(), 0, 'Devices must not show an approval queue');
  assert.equal(await account.getByText(/Ask the operator to enable it/).count(), 0);
  assert.deepEqual(pageErrors, [], 'Bundled UI raised JavaScript errors');
  console.log('Bundled UI completed local setup and enabled first account linking without account, remote worker, or provider credentials.');
} finally {
  await page.screenshot({ path: '/tmp/container-ui.png', mask: [page.locator('code')] });
  await context.close();
  await browser.close();
}
