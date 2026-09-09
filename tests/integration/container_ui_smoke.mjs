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
  const create = page.getByRole('button', { name: /^Start fresh/ });
  if (await create.isVisible()) {
    await create.click();
  }
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
  await page.getByText('Optional — connect your development tools on this machine', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Skip for now', exact: true }).click();
  await page.getByText('Add AI providers', { exact: true }).waitFor();
  const completed = page.waitForResponse(response => response.url().endsWith('/auth/onboarding/complete') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Finish Setup', exact: true }).click();
  assert.equal((await completed).status(), 200, 'Standalone onboarding completion was rejected');
  await page.waitForURL(url => url.origin === 'http://127.0.0.1:8000' && url.pathname !== '/setup');
  assert.deepEqual(pageErrors, [], 'Bundled UI raised JavaScript errors');
  console.log('Bundled UI completed local setup without account, remote worker, or provider credentials.');
} finally {
  await page.screenshot({ path: '/tmp/container-ui.png', mask: [page.locator('code')] });
  await context.close();
  await browser.close();
}
