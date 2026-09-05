import assert from 'node:assert/strict';

// Real component + stream client + canvas; only HTTP and WebSocket are fixtures.
export async function verifyProviderLogin(browser, fixtureUrl, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [], unexpected = [], calls = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  const initial = { status: 'not_connected', flow_id: null, observation_id: null,
    organization_id: null, credits: null, fetched_at: null, error_kind: null,
    owner_instance_id: null, browser_available: true };
  let status = initial;
  await page.route('**/*', async route => {
    const request = route.request(), target = new URL(request.url());
    if (target.pathname.startsWith('/api/')) {
      const action = target.pathname.split('/').at(-1);
      calls.push({ path: target.pathname, method: request.method(), body: request.postDataJSON() });
      if (!target.pathname.startsWith('/api/v1/model-providers/p1/openai-billing/') ||
          !['status', 'connect', 'cancel'].includes(action)) {
        unexpected.push(request.url()); return route.abort();
      }
      if (action === 'connect') status = { ...initial, status: 'pending', flow_id: 'flow-1' };
      if (action === 'cancel') status = initial;
      return route.fulfill({ json: status });
    }
    if (target.origin !== new URL(fixtureUrl).origin) {
      unexpected.push(request.url()); return route.abort();
    }
    return route.continue();
  });
  try {
    const url = new URL(fixtureUrl); url.searchParams.set('provider-login', '1');
    await page.goto(url.href);
    await page.getByRole('button', { name: 'Sign in for credits', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Sign in for OpenAI credits' });
    await dialog.waitFor();
    assert.equal(await dialog.evaluate(el => el.matches(':modal')), true);
    await page.waitForFunction(() => window.browserFixture.loginOpened);
    // Let initial onOpen/ResizeObserver attempts run before supplying hello.
    await page.waitForTimeout(150);
    assert.deepEqual(await page.evaluate(() => window.browserFixture.messages), [], 'pre-hello viewport must be rejected');
    assert.equal(new URL(await page.evaluate(() => window.browserFixture.loginSocketUrl)).pathname,
      '/api/v1/model-providers/p1/openai-billing/browser');
    await page.evaluate(() => window.browserFixture.loginHello());
    assert.deepEqual(await page.evaluate(() => window.browserFixture.messages), [], 'hello alone must not resize');
    await page.evaluate(() => window.browserFixture.loginLive());
    await page.waitForFunction(() => window.browserFixture.messages.some(m => m.type === 'viewport'));
    const dimensions = await dialog.locator('.browser').evaluate(el => {
      const rect = el.getBoundingClientRect(); return { width: Math.round(rect.width), height: Math.round(rect.height) };
    });
    const commands = await page.evaluate(() => window.browserFixture.messages);
    assert.deepEqual(commands[0], { type: 'viewport', ...dimensions }, 'live resends the rejected initial viewport');
    const canvas = dialog.locator('canvas');
    await page.waitForFunction(() => {
      const c = document.querySelector('canvas');
      return c?.width > 0 && c.getContext('2d').getImageData(c.width / 2, c.height / 2, 1, 1).data[3] > 0;
    });
    await canvas.click();
    await page.waitForFunction(() => window.browserFixture.messages.some(m => m.type === 'input'));
    assert.equal(await page.evaluate(() => document.activeElement?.tagName === 'TEXTAREA'), false, 'page taps do not open the keyboard');
    await dialog.getByRole('button', { name: 'Show keyboard', exact: true }).click();
    await page.waitForFunction(() => document.activeElement?.tagName === 'TEXTAREA');
    await page.keyboard.insertText('fixture login');
    await page.waitForFunction(() => window.browserFixture.messages.some(m => m.type === 'input' && m.kind === 'text' && m.text === 'fixture login'));
    await canvas.click();
    await page.waitForFunction(() => document.activeElement?.tagName === 'TEXTAREA');
    const toggle = dialog.getByRole('button', { name: 'Hide keyboard', exact: true });
    assert.equal(await toggle.getAttribute('aria-pressed'), 'true');
    const toggleBox = await toggle.boundingBox(), browserBox = await dialog.locator('.browser').boundingBox();
    assert.ok(toggleBox && browserBox && toggleBox.y + toggleBox.height <= browserBox.y, 'keyboard toggle remains outside browser content');
    await toggle.click();
    await page.waitForFunction(() => document.activeElement?.tagName !== 'TEXTAREA');
    assert.equal(await dialog.getByRole('button', { name: 'Show keyboard', exact: true }).getAttribute('aria-pressed'), 'false');
    const text = await page.locator('body').innerText();
    assert.equal(text.includes('auth.example.test'), false);
    assert.equal(text.includes('never-display'), false);
    assert.equal(await page.getByRole('textbox', { name: 'Page address' }).count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await dialog.evaluate(el => el.scrollWidth > el.clientWidth), false);
    const screenshot = `/tmp/provider-login-${viewport.width}.png`;
    await page.screenshot({ path: screenshot });
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(await page.evaluate(() => window.browserFixture.openSockets), 0);
    assert.ok(calls.some(call => call.method === 'POST' && call.path.endsWith('/cancel') && call.body.flow_id === 'flow-1'));
    assert.deepEqual(unexpected, []);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ providerLogin: true, viewport, protocol: 1, modal: true,
      preHelloViewport: 'rejected', liveViewport: dimensions, bridgeInput: true,
      privateUrlHidden: true, stickyKeyboard: true, headerKeyboardToggle: true, overflow: false, pageErrors: errors, screenshot }));
  } finally { await page.close(); }
}
