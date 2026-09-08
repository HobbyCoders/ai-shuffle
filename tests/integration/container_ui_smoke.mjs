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
  const create = page.getByRole('button', { name: /Start a new worker/ });
  if (await create.isVisible()) {
    await create.click();
    await page.getByLabel('Network name (optional)').fill('Disposable container check');
    await page.getByRole('button', { name: 'Create network', exact: true }).click();
    await page.getByText('Save your recovery key', { exact: true }).waitFor();
    // This is a disposable local-only network; never log or publish its key.
    await page.getByLabel('I saved this key somewhere safe.').check();
    await page.getByRole('button', { name: 'Continue', exact: true }).click();
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
  await page.getByText('Connect your development tools', { exact: true }).waitFor();
  assert.deepEqual(pageErrors, [], 'Bundled UI raised JavaScript errors');
  console.log('Bundled UI rendered; network/workspace onboarding persisted through real APIs.');
} finally {
  await page.screenshot({ path: '/tmp/container-ui.png', mask: [page.locator('code')] });
  await context.close();
  await browser.close();
}
