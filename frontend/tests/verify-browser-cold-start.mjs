import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export async function verifyColdStart(browser, base, viewport) {
 const page = await browser.newPage({ viewport });
 const errors = [], consoleErrors = [], requests = [];
 page.on('pageerror', e => errors.push(e.message));
 page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
 page.on('request', r => { if (r.method() === 'POST') requests.push(r.url()); });
 const cold = base + '?cold';
 const address = () => page.getByRole('textbox', { name: 'Page address', exact: true });
 const edit = async value => { if (!await address().count()) await page.locator('.address-display').click(); await address().fill(value); };
 const enter = () => address().press('Enter');
 const fixture = fn => page.evaluate(fn);
 try {
  await page.goto(cold);
  await page.locator('.address-display').waitFor();
  assert.equal(await page.locator('.tab.current').innerText(), 'New tab');
  await page.screenshot({ path: join(tmpdir(), `browser-cold-blank-${viewport.width}.png`) });
  assert.equal(await page.locator('.tab .icon').count(), 0);
  await fixture(() => { browserFixture.failStart(); browserFixture.holdStart(true); });
  await edit('first.test'); await enter(); await enter();
  assert.equal(await fixture(() => browserFixture.starts.length), 1, 'rapid Enter has one start');
  await fixture(() => browserFixture.releaseStart());
  await page.getByRole('alert').filter({ hasText: 'Browser could not start. Try again.' }).waitFor();
  assert.equal(await address().inputValue(), 'first.test');
  assert.equal(await address().evaluate(el => el === document.activeElement), true);
  await fixture(() => { browserFixture.holdTakeover(true); browserFixture.holdWorkspace(true); });
  await enter();
  await page.waitForFunction(() => browserFixture.starts.length === 2 && browserFixture.state.available);
  assert.equal(await fixture(() => browserFixture.messages.some(m => m.op === 'takeover' || m.op === 'navigate')), false, 'HTTP ready does not replace WS readiness');
  await fixture(() => browserFixture.releaseWorkspace());
  await page.waitForFunction(() => browserFixture.messages.some(m => m.op === 'takeover'));
  assert.equal(await fixture(() => browserFixture.messages.some(m => m.op === 'navigate')), false);
  await fixture(() => browserFixture.grantControl(false));
  // Authoritative ownership alone is not the explicit takeover acknowledgement.
  await page.waitForFunction(() => browserFixture.state.control.owner === 'human');
  assert.equal(await fixture(() => browserFixture.messages.some(m => m.op === 'navigate')), false);
  await fixture(() => browserFixture.grantControl(true));
  await page.waitForFunction(() => browserFixture.messages.some(m => m.op === 'navigate'));
  await page.locator('.address-display').waitFor();
  assert.equal(await fixture(() => browserFixture.starts.length), 2);
  assert.equal(await fixture(() => browserFixture.state.tabs.length), 1);
  assert.equal(await page.locator('.tab .icon').count(), 0);
  const start = await fixture(() => browserFixture.starts[0]);
  assert.equal(start.method, 'POST'); assert.equal(start.credentials, 'include'); assert.equal(start.body, undefined);
  assert.ok(start.url.endsWith('/sessions/isolated-fixture/browser/panel/start'));
  const nav = await fixture(() => browserFixture.messages.find(m => m.op === 'navigate'));
  assert.equal(nav.args.url, 'https://first.test'); assert.equal(nav.runtime_id, 'fixture-runtime'); assert.equal(nav.tab_id, 'initial-tab');
  await page.getByRole('button', { name: 'New tab', exact: true }).click();
  await page.waitForFunction(() => browserFixture.state.tabs.length === 2);
  assert.equal(await page.locator('.tab .icon').count(), 2);
  await page.locator('.tab .icon').last().click();
  await page.waitForFunction(() => browserFixture.state.tabs.length === 1);
  assert.equal(await page.locator('.tab .icon').count(), 0);
  for (const [label, command] of [['Go back','back'], ['Go forward','forward'], ['Reload page','reload']]) {
   await page.getByRole('button', { name: label, exact: true }).click();
   assert.deepEqual(await fixture(() => browserFixture.messages.at(-1).args), { command });
  }
  await fixture(() => browserFixture.page(false));
  const before = await page.locator('.panel-header').boundingBox();
  for (let i = 0; i < 4; i++) await page.evaluate(i => browserFixture.churn(i), i);
  assert.equal(await page.getByRole('button', { name: 'Reload page', exact: true }).count(), 1);
  assert.deepEqual(await page.locator('.panel-header').boundingBox(), before, 'busy metadata does not shift toolbar');
  await fixture(() => browserFixture.holdNavigation(true));
  await edit('slow.test'); await enter();
  await page.getByRole('button', { name: 'Stop loading', exact: true }).waitFor();
  await fixture(() => { browserFixture.page(false); browserFixture.churn(0); });
  assert.equal(await page.getByRole('button', { name: 'Stop loading', exact: true }).isEnabled(), true, 'pending navigation remains interruptible');
  await page.screenshot({ path: join(tmpdir(), `browser-cold-loading-${viewport.width}.png`) });
  await page.getByRole('button', { name: 'Stop loading', exact: true }).click();
  await page.getByRole('button', { name: 'Reload page', exact: true }).waitFor();
  assert.deepEqual(await fixture(() => browserFixture.messages.at(-1).args), { command: 'stop' });
  await fixture(() => { browserFixture.holdNavigation(false); browserFixture.failNavigation(); });
  await edit('retry.test'); await enter();
  await page.getByRole('alert').filter({ hasText: 'Navigation failed. Try again.' }).waitFor();
  assert.equal(await address().inputValue(), 'retry.test'); assert.equal(await address().evaluate(el => el === document.activeElement), true);
  await enter(); await page.locator('.address-display').waitFor();
  await fixture(() => browserFixture.otherOwner());
  const commandCount = await fixture(() => browserFixture.messages.length);
  await edit('not-yours.test'); await enter();
  await page.getByRole('alert').filter({ hasText: 'Another viewer controls this browser. Ask them to return control, then try again.' }).waitFor();
  assert.equal(await fixture(() => browserFixture.messages.length), commandCount);
  assert.equal(await address().inputValue(), 'not-yours.test');
  const bounds = await page.locator('.browser-panel').boundingBox();
  const errorBounds = await page.getByRole('alert').boundingBox();
  const headerBounds = await page.locator('.panel-header').boundingBox();
  assert.ok(errorBounds.y >= headerBounds.y + headerBounds.height, 'errors never cover navigation or top-right close');
  const screenshot = join(tmpdir(), `browser-cold-start-${viewport.width}.png`);
  await page.screenshot({ path: screenshot });
  // Pending HTTP deliberately ignores AbortSignal: the UI must still ignore late completion.
  for (const action of ['switch', 'close']) {
   await page.goto(cold); await page.locator('.address-display').waitFor();
   await fixture(() => browserFixture.holdStart(true));
   await edit('late.test'); await enter();
   await page.waitForFunction(() => browserFixture.starts.length === 1);
   if (action === 'switch') await fixture(() => { browserFixture.openSession('session-B'); browserFixture.setSession('session-B'); });
   else await page.getByRole('button', { name: 'Close browser panel', exact: true }).click();
   await fixture(() => browserFixture.releaseStart());
   await page.waitForFunction(() => browserFixture.state.available);
   // Flush the promise chain and the Svelte update without an arbitrary sleep.
   await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
   assert.equal(await fixture(() => browserFixture.messages.some(m => m.op === 'navigate' || m.op === 'takeover')), false, `${action}: no late commands`);
  }
  await page.goto(cold); await page.getByRole('button', { name: 'New tab', exact: true }).click();
  await page.waitForFunction(() => browserFixture.state.control.owner === 'human');
  assert.equal(await fixture(() => browserFixture.starts.length), 1);
  assert.equal(await fixture(() => browserFixture.state.tabs.length), 1);
  assert.equal(await fixture(() => browserFixture.messages.some(m => m.op === 'tabs')), false, 'cold + uses bootstrap tab');
  assert.equal(await fixture(() => browserFixture.messages.some(m => m.type === 'message' || m.type === 'chat')), false);
  assert.deepEqual(requests, [], 'fixture start uses mocked fetch; no chat POSTs escape');
  assert.deepEqual(errors, []); assert.deepEqual(consoleErrors, []);
  console.log(JSON.stringify({ coldStart: true, viewport, bounds, screenshot, retry: true, duplicateStart: false, takeoverAck: true, staleCompletionIgnored: ['switch','close'], navigation: ['back','forward','reload','stop','retry'], busyLoadingStable: true, firstTab: true, pageErrors: errors, consoleErrors }));
 } catch (error) { console.error('Cold-start failure state', await page.locator('body').innerText()); throw error; } finally { await page.close(); }
}
