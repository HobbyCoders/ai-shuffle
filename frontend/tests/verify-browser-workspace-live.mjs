#!/usr/bin/env node
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { isDeepStrictEqual } from 'node:util';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const require = createRequire(new URL('../../browser-mcp/package.json', import.meta.url));
const { chromium } = require('playwright-core');
const config = {
  api: process.env.BROWSER_WORKSPACE_FIXTURE_API,
  session: process.env.BROWSER_WORKSPACE_FIXTURE_SESSION,
  token: process.env.BROWSER_WORKSPACE_FIXTURE_TOKEN,
};
let server, browser, primary, secondary, originalId;
let phase = 'configuration';
const button = (page, name) => page.getByRole('button', { name, exact: true });
const wait = (page, predicate, arg) => page.waitForFunction(predicate, arg, { timeout: 20_000 });
const state = (page) => page.evaluate(() => window.browserFixture.state);
async function mirrored(predicate, arg) {
  await Promise.all([primary, secondary].map(page => wait(page, predicate, arg)));
  // A title/page event may arrive between the two reads. Wait for convergence
  // rather than treating two adjacent revisions as an ownership/tab mismatch.
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (isDeepStrictEqual(await state(primary), await state(secondary))) return;
    await primary.waitForTimeout(50);
  }
  assert.fail('Viewers did not converge');
}
async function painted(page, width, height) {
  try {
  await wait(page, ({ width, height }) => {
    const fixture = window.browserFixture;
    const canvas = document.querySelector('canvas');
    if (!fixture.frames || !canvas || !canvas.width || !canvas.height) return false;
    if (fixture.frameMetadata?.tab_id !== fixture.state.active_tab_id) return false;
    // Canvas backing pixels are viewer-scaled: compare CSS-page metadata, not
    // canvas.width, which can legitimately be a thumbnail-sized projection.
    if (width && (canvas.dataset.viewportWidth !== String(width) ||
        canvas.dataset.viewportHeight !== String(height))) return false;
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const first = pixels.slice(0, 3).join(',');
    for (let i = 0; i < pixels.length; i += 4 * 97) {
      if (pixels[i + 3] && pixels.slice(i, i + 3).join(',') !== first) return true;
    }
    return false;
  }, { width, height });
  } catch (error) {
    console.error('Live paint readiness', JSON.stringify(await page.evaluate(() => {
      const canvas = document.querySelector('canvas');
      return {frames: window.browserFixture.frames, metadata: window.browserFixture.frameMetadata,
        viewport: window.browserFixture.state.viewport,
        canvas: canvas && {width:canvas.width,height:canvas.height,dataset:{...canvas.dataset},
          rect:canvas.getBoundingClientRect().toJSON()},
        text:document.body.innerText.slice(0,350)};
    })));
    throw error;
  }
}
async function closeExtraTabs() {
  const current = await state(primary);
  for (const tab of current.tabs.filter(tab => tab.id !== originalId)) {
    const index = (await state(primary)).tabs.findIndex(item => item.id === tab.id);
    await primary.locator('.tabs .tab').nth(index).getByRole('button', { name: /^Close / }).click();
    await mirrored(() => window.browserFixture.state.tabs.length === 1);
  }
}
try {
  assert.ok(config.api && config.session && config.token);
  assert.ok(['http:', 'https:'].includes(new URL(config.api).protocol));
  phase = 'fixture server';
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    configFile: fileURLToPath(new URL('./vite.browser-workspace.config.js', import.meta.url)),
    logLevel: 'silent',
    server: { host: '127.0.0.1', port: 0, strictPort: false, open: false },
  });
  await server.listen();
  const address = server.httpServer.address();
  assert.ok(address && typeof address !== 'string');
  const url = `http://127.0.0.1:${address.port}/tests/browser-workspace.html`;
  phase = 'fit and session-update regressions';
  await new Promise((resolve, reject) => {
    const regression = spawn(process.execPath,
      [fileURLToPath(new URL('./verify-browser-workspace.mjs', import.meta.url))],
      { env: { ...process.env, BROWSER_FIXTURE_URL: url }, stdio: 'inherit', timeout: 90_000 });
    regression.once('error', reject);
    regression.once('close', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`Browser UI regressions failed: ${signal ?? code}`));
    });
  });
  phase = 'real browser viewers';
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
    headless: true, args: ['--no-sandbox', '--disable-gpu'],
  });
  let pageErrors = 0;
  async function viewer(viewport) {
    const page = await browser.newPage({ viewport });
    page.setDefaultTimeout(20_000);
    page.on('pageerror', error => {
      pageErrors++;
      console.error('Fixture page error:', String(error.message).replaceAll(config.token, '[fixture-token]').slice(0, 700));
    });
    page.on('response', response => {
      if (response.status() >= 400) console.error('Fixture HTTP error:', response.status(), new URL(response.url()).pathname);
    });
    // HTTP shell/preferences stay isolated from the parent's database. This
    // routing does not intercept WebSockets: those go to the real API origin.
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname;
      let body = {};
      if (path.endsWith('/preferences/all')) body = { preferences: {} };
      else if (path.includes('/preferences/')) {
        const value = route.request().method() === 'PUT'
          ? route.request().postDataJSON()?.value : null;
        body = { key: decodeURIComponent(path.split('/').pop()), value };
      } else if (path.includes('browser')) body = { enabled: true };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.addInitScript(value => { window.browserWorkspaceLive = value; }, config);
    await page.goto(url);
    try {
      await wait(page, () => !!window.browserFixture?.viewerId && window.browserFixture.state.available);
    } catch (error) {
      console.error('Live fixture readiness', JSON.stringify(await page.evaluate(() => ({
        fixture: !!window.browserFixture,
        viewer: !!window.browserFixture?.viewerId,
        available: window.browserFixture?.state.available,
        frames: window.browserFixture?.frames,
        text: document.body.innerText.slice(0, 500),
      }))));
      throw error;
    }
    return page;
  }
  phase = 'initial real stream';
  primary = await viewer({ width: 1440, height: 900 });
  const initial = await state(primary);
  assert.equal(initial.tabs.length, 1);
  assert.equal(initial.control.owner, 'agent');
  originalId = initial.tabs[0].id;
  const initialViewport = initial.viewport;
  secondary = await viewer({ width: 900, height: 700 });
  await mirrored(() => window.browserFixture.state.control.owner === 'agent');
  assert.deepEqual((await state(secondary)).viewport, initialViewport);
  assert.notEqual(await primary.evaluate(() => window.browserFixture.viewerId),
    await secondary.evaluate(() => window.browserFixture.viewerId));
  await Promise.all([painted(primary), painted(secondary)]);
  const screenshots = [];
  for (const [name, viewport] of [
    ['desktop', { width: 1440, height: 900 }],
    ['mobile', { width: 390, height: 844 }],
  ]) {
    phase = `${name} ownership`;
    await primary.setViewportSize(viewport);
    assert.equal(await button(primary, 'New tab').isDisabled(), true);
    await button(primary, 'Take control').click();
    const ownerId = await primary.evaluate(() => window.browserFixture.viewerId);
    await mirrored(() => window.browserFixture.state.control.owner === 'human');
    assert.equal((await state(secondary)).control.viewer_id, ownerId);
    await secondary.getByRole('status').filter({ hasText: 'Another viewer controls' }).waitFor();
    assert.equal(await button(secondary, 'Take control').isDisabled(), true);
    assert.equal(await button(secondary, 'New tab').isDisabled(), true);

    phase = `${name} tabs and navigation`;
    await button(primary, 'New tab').click();
    await mirrored(() => window.browserFixture.state.tabs.length === 2);
    const newId = (await state(primary)).active_tab_id;
    assert.notEqual(newId, originalId);
    const destination = new URL(`/fixture?viewer=${name}`, config.api).href;
    await primary.locator('.address-display').click();
    const addressInput = primary.getByRole('textbox', { name: 'Page address', exact: true });
    await addressInput.fill(destination);
    await addressInput.press('Enter');
    await mirrored(url => window.browserFixture.state.tabs.some(tab => tab.current && tab.url === url), destination);
    await painted(primary);
    await painted(secondary);

    phase = `${name} viewport and projection`;
    if (!(await primary.locator('details.viewport').evaluate(element => element.open))) {
      await primary.locator('details.viewport summary').click();
    }
    await primary.getByLabel('Viewport preset', { exact: true }).selectOption('390x844');
    await mirrored(() => window.browserFixture.state.viewport.width === 390 && window.browserFixture.state.viewport.height === 844);
    await Promise.all([painted(primary, 390, 844), painted(secondary, 390, 844)]);
    await button(primary, 'Rotate viewport').click();
    await mirrored(() => window.browserFixture.state.viewport.width === 844 && window.browserFixture.state.viewport.height === 390);
    await painted(primary, 844, 390);
    await button(primary, 'Rotate viewport').click();
    await mirrored(() => window.browserFixture.state.viewport.width === 390 && window.browserFixture.state.viewport.height === 844);
    const counts = await Promise.all([primary, secondary].map(page => page.evaluate(() => window.browserFixture.messages.filter(m => m.op === 'viewport').length)));
    await primary.evaluate(() => window.browserFixture.setWidth(640));
    await primary.setViewportSize({ width: viewport.width + 24, height: viewport.height - 20 });
    await secondary.evaluate(() => window.browserFixture.setWidth(360));
    await secondary.setViewportSize({ width: 510, height: 650 });
    // Wait beyond the panel's debounced resize path before checking no command.
    await primary.waitForTimeout(750);
    await primary.setViewportSize(viewport);
    await primary.waitForTimeout(750);
    await mirrored(() => window.browserFixture.state.viewport.mode === 'fixed' && window.browserFixture.state.viewport.width === 390 && window.browserFixture.state.viewport.height === 844);
    assert.deepEqual(await Promise.all([primary, secondary].map(page => page.evaluate(() => window.browserFixture.messages.filter(m => m.op === 'viewport').length))), counts);
    await Promise.all([painted(primary, 390, 844), painted(secondary, 390, 844)]);
    assert.equal(await primary.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const screenshot = `/tmp/browser-workspace-live-${name}.png`;
    await primary.screenshot({ path: screenshot });
    screenshots.push(screenshot);

    phase = `${name} restore tabs and agent control`;
    await closeExtraTabs();
    await mirrored(id => window.browserFixture.state.active_tab_id === id, originalId);
    await button(primary, 'Return control').click();
    await mirrored(() => window.browserFixture.state.control.owner === 'agent' && !window.browserFixture.state.control.viewer_id);
    assert.equal(await button(primary, 'New tab').isDisabled(), true);
    assert.equal(await button(secondary, 'New tab').isDisabled(), true);
  }
  assert.equal(pageErrors, 0);
  for (const page of [primary, secondary]) {
    assert.equal(await page.evaluate(() => window.browserFixture.commandFailures), 0);
  }
  console.log(JSON.stringify({ pass: true, views: ['desktop', 'mobile'], realFrames: true,
    mirroredControl: true, mirroredTabs: true, fixedViewport: true,
    passiveViewer: true, finalTabs: 1, finalControl: 'agent', screenshots }));
} catch (error) {
  console.error('Backend stream state:', await (await fetch(config.api + '/fixture-stream-state')).text());
  const detail = String(error?.message ?? error).replaceAll(config.token || '\0', '[fixture-token]')
    .replace(/wss?:\/\/[^\s"']+/g, '[workspace-socket]').slice(0, 1200);
  console.error(`Browser workspace live verification failed: ${phase}: ${detail}`);
  process.exitCode = 1;
} finally {
  try {
    if (primary && secondary && originalId && !primary.isClosed()) {
      const current = await state(primary);
      const ownId = await primary.evaluate(() => window.browserFixture.viewerId);
      if (current.control.viewer_id === ownId) {
        await closeExtraTabs();
        await button(primary, 'Return control').click();
        await mirrored(() => window.browserFixture.state.control.owner === 'agent');
      }
    }
  } catch {
    console.error('Browser workspace live cleanup could not restore workspace');
    process.exitCode = 1;
  } finally {
    try { await browser?.close(); }
    catch { console.error('Viewer browser cleanup failed'); process.exitCode = 1; }
    try { await server?.close(); }
    catch { console.error('Fixture server cleanup failed'); process.exitCode = 1; }
  }
}
