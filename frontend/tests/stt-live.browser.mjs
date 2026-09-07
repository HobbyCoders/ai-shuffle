import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/opt/browser-mcp/node_modules/playwright-core');
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium' });
try {
 for (const width of [1440, 390]) {
  const page = await browser.newPage({ viewport: { width, height: 844 }, hasTouch: width === 390 });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.route(url => !['127.0.0.1', 'localhost'].includes(url.hostname) || url.pathname.startsWith('/api/'), route => route.abort());
  await page.goto(process.env.FIXTURE_URL || 'http://127.0.0.1:5198/tests/composer-suggestion.html?stt');
  await page.waitForFunction(() => !!window.sttFixture && !!window.suggestionFixture);
  const editor = page.locator('[contenteditable=true]');
  await editor.fill('Original draft.');
  await page.evaluate(() => window.sttFixture.scenario('reject'));
  await page.getByRole('button', { name: 'Start voice input', exact: true }).click();
  // Model rejection must recover before the unanswered permission request.
  await page.waitForFunction(() => document.body.textContent.includes('GPT Live Transcribe is unavailable for this account.'));
  assert.equal(await page.getByRole('button', { name: 'Start voice input', exact: true }).isEnabled(), true);
  await page.evaluate(() => window.sttFixture.releaseMic());
  await page.waitForFunction(() => window.sttFixture.stoppedTracks === 1);
  assert.equal(await editor.textContent(), 'Original draft.');
  await page.evaluate(() => window.sttFixture.scenario('success'));
  await page.getByRole('button', { name: 'Start voice input', exact: true }).click();
  await page.getByRole('button', { name: 'Stop voice input', exact: true }).waitFor();
  await page.evaluate(() => {
   window.sttFixture.emit({ type: 'item_started', item_id: 'a' });
   for (const text of ['Hello', ' world']) window.sttFixture.emit({ type: 'delta', item_id: 'a', text });
  });
  await page.waitForFunction(() => document.querySelector('[contenteditable=true]').textContent.includes('Hello world'));
  await page.getByRole('button', { name: 'Stop voice input', exact: true }).click();
  await page.waitForFunction(() => window.sttFixture.stoppedTracks === 2);
  assert.equal((await editor.textContent()).match(/Hello world\./g)?.length, 1);
  assert.ok((await editor.textContent()).includes('Original draft.'));
  await page.getByRole('button', { name: 'Start voice input', exact: true }).click();
  await page.getByRole('button', { name: 'Stop voice input', exact: true }).waitFor();
  await page.evaluate(() => window.sttFixture.emit({ type: 'error', fatal: true, message: 'GPT Live Transcribe connection lost.' }));
  await page.waitForFunction(() => window.sttFixture.stoppedTracks === 3);
  assert.equal(await page.getByRole('button', { name: 'Start voice input', exact: true }).isEnabled(), true);
  assert.equal(await page.evaluate(() => window.suggestionFixture.sends), 0);
  assert.equal(await page.evaluate(() => window.sttFixture.requests.some(url => /transcribe$/.test(url))), false);
  assert.deepEqual(errors, []);
  if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/stt-live-${width}.png`, fullPage: true });
  console.log(JSON.stringify({ width, rejectedModelRecovered: true, lateMicReleased: true, partialsAndFinal: true, fatalRecovered: true, pageErrors: errors, modelCalls: 0 }));
  await page.close();
 }
} finally { await browser.close(); }
