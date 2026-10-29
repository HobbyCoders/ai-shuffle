import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Render retained xAI controls with synthetic reads, never live account data or billing.
export async function verifyProviderBalance(browser, fixtureUrl, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [], unexpected = [], calls = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('popup', popup => unexpected.push(`popup: ${popup.url()}`));
  await page.context().route('**/*', async route => {
    const request = route.request(), target = new URL(request.url());
    if (target.pathname.startsWith('/api/')) {
      calls.push({ path: target.pathname, method: request.method() });
      if (request.method() === 'GET' && target.pathname === '/api/v1/model-providers/fixture-xai/balance-access') {
        return route.fulfill({ json: { configured: false, source: 'fixture', source_id: 'xai_management',
          fetched_at: null, fresh_until: null, refreshing: false, last_error_kind: null,
          needs_reauth: false, status: 'unconfigured', credits: null } });
      }
      return route.abort();
    }
    if (target.origin !== new URL(fixtureUrl).origin || request.method() !== 'GET') {
      unexpected.push(request.url()); return route.abort();
    }
    return route.continue();
  });
  try {
    // Mount-time timers must use the controllable clock, not the native one.
    await page.clock.install();
    const url = new URL(fixtureUrl); url.searchParams.set('provider-balance', '1');
    await page.goto(url.href);
    const usage = page.getByRole('region', { name: 'xAI Management API balance', exact: true });
    await usage.getByRole('heading', { name: 'xAI Management API balance', exact: true }).waitFor();
    assert.match(await usage.innerText(), /Create a management key in console.x.ai/);

    const assertPassive = async () => {
      assert.equal(await page.locator('input').count(), 1);
      assert.equal(await page.locator('textarea, select, form').count(), 0);
      const key = page.getByLabel('xAI management key', { exact: true });
      assert.equal(await key.inputValue(), '');
      assert.equal(await key.getAttribute('type'), 'password');
      assert.equal(await page.getByRole('button', { name: 'Save access', exact: true }).isDisabled(), true);
      assert.equal(await page.getByRole('button', { name: 'Disconnect balance', exact: true }).isDisabled(), true);
      assert.equal(await page.getByRole('dialog').count(), 0);
      assert.deepEqual(await page.evaluate(() => ({
        sockets: window.browserFixture.socketCount,
        openSockets: window.browserFixture.openSockets,
        starts: window.browserFixture.starts,
        messages: window.browserFixture.messages,
      })), { sockets: 0, openSockets: 0, starts: [], messages: [] });
      assert.equal(page.context().pages().length, 1, 'no login/billing browser opened');
      assert.equal(page.url(), url.href, 'no automatic billing navigation');
      assert.deepEqual(calls, [
        { path: '/api/v1/model-providers/fixture-xai/balance-access', method: 'GET' },
      ], 'no background reads, credential writes or disconnects');
      assert.deepEqual(unexpected, []);
      assert.deepEqual(errors, []);
    };
    await assertPassive();
    await page.clock.fastForward(60_000);
    await assertPassive();
    assert.equal(await usage.evaluate(el => el.scrollWidth > el.clientWidth), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight), false);
    const screenshot = join(tmpdir(), `provider-balance-${viewport.width}.png`);
    await page.screenshot({ path: screenshot });
    console.log(JSON.stringify({ providerBalance: true, viewport,
      writeOnlyAccessInput: true, apiCalls: calls, automaticPolling: false,
      loginBrowser: false, overflow: false, pageErrors: errors, screenshot }));
  } finally { await page.context().close(); }
}

// A real pre-mount interval must be detected, not hidden by late clock installation.
export async function verifyProviderPollingCanary(browser, fixtureUrl, viewport) {
  const requests = [];
  const pollingBrowser = {
    async newPage(options) {
      const page = await browser.newPage(options);
      page.on('request', request => {
        const path = new URL(request.url()).pathname;
        if (path.startsWith('/api/')) requests.push({ path, method: request.method() });
      });
      // Hold the real fixture entry module until its pre-mount timer is scheduled.
      // This avoids init-script ordering against Playwright's clock installation;
      // continue the unmodified source, never replace the HTML or component.
      await page.route(new URL('browser-workspace.ts*', fixtureUrl).href, async route => {
        assert.equal(await page.evaluate(() => {
          const beforeMount = !document.querySelector('#app section');
          setInterval(() => {
            void fetch('/api/v1/model-providers/p1/balance-access').catch(() => {});
          }, 30_000);
          return beforeMount;
        }), true, 'the polling canary must be scheduled before component mount');
        return route.continue();
      });
      return page;
    },
  };
  await assert.rejects(verifyProviderBalance(pollingBrowser, fixtureUrl, viewport), {
    code: 'ERR_ASSERTION', message: /no background reads, credential writes or disconnects/,
  }, 'the verifier must reject polling scheduled before component mount');
  const polling = requests.filter(request => request.path === '/api/v1/model-providers/p1/balance-access');
  assert.ok(polling.length > 0, 'the polling canary must actually attempt its extra request');
  for (const request of polling) {
    assert.deepEqual(request, { path: '/api/v1/model-providers/p1/balance-access', method: 'GET' });
  }
  console.log(JSON.stringify({ providerPollingCanary: true, viewport, detectedRequests: requests }));
}
