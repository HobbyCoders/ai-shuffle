import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Render current usage controls with synthetic reads, never live account data or billing.
export async function verifyProviderLogin(browser, fixtureUrl, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [], unexpected = [], calls = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('popup', popup => unexpected.push(`popup: ${popup.url()}`));
  // Context routing also blocks the first request of any accidental billing popup.
  await page.context().route('**/*', async route => {
    const request = route.request(), target = new URL(request.url());
    if (target.pathname.startsWith('/api/')) {
      calls.push({ path: target.pathname, method: request.method() });
      if (request.method() === 'GET' && target.pathname === '/api/v1/model-providers/fixture-openai/balance-access') {
        return route.fulfill({ json: { configured: false, source: 'fixture', source_id: 'fixture',
          fetched_at: null, fresh_until: null, refreshing: false, last_error_kind: null,
          needs_reauth: false, status: 'unconfigured', credits: null } });
      }
      if (request.method() === 'GET' && target.pathname === '/api/v1/model-providers/fixture-openai/openai-usage') {
        return route.fulfill({ json: { configured: false, manual_balance: null, organization_id: null,
          source: 'fixture', scope: 'fixture', spend: null, fetched_at: null, last_error_kind: null } });
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
    const url = new URL(fixtureUrl); url.searchParams.set('provider-login', '1');
    await page.goto(url.href);
    // Settings > Providers > OpenAI API key > Usage.
    const usage = page.getByRole('region', { name: 'Manual balance (fallback only)', exact: true });
    await usage.getByRole('heading', { name: 'Manual balance (fallback only)', exact: true }).waitFor();
    assert.match(await usage.innerText(), /This is not a provider reading/);
    await page.getByRole('region', { name: 'OpenAI Platform balance (unofficial)', exact: true }).waitFor();
    // The guidance lives in the row's help tip; showing it is local only.
    const help = usage.getByRole('button', { name: 'About Balance', exact: true });
    await help.focus();
    const guidance = await page.getByRole('tooltip').innerText();
    assert.match(guidance, /OpenAI does not expose a prepaid balance through the model API key/);
    assert.match(guidance, /An Admin key reports organization spending, not credit left/);
    assert.match(guidance, /ChatGPT credits are separate from API billing/);
    await help.blur();

    // Inspect the official link without clicking it or making a billing request.
    const billing = usage.getByRole('link', { name: 'Open billing', exact: true });
    assert.equal(await billing.isVisible(), true);
    assert.equal(await billing.getAttribute('href'), 'https://platform.openai.com/settings/organization/billing/overview');
    assert.equal(await billing.getAttribute('target'), '_blank');
    const rel = (await billing.getAttribute('rel') ?? '').split(/\s+/);
    assert.ok(rel.includes('noopener'));
    assert.ok(rel.includes('noreferrer'));

    const assertPassive = async () => {
      // Current main intentionally offers write-only Platform access and a
      // manual-balance fallback. Mounting may read status, never use those actions.
      assert.equal(await page.locator('input').count(), 2);
      assert.equal(await page.locator('textarea, select, form').count(), 0);
      const key = page.getByLabel('Platform session key', { exact: true });
      assert.equal(await key.inputValue(), '');
      assert.equal(await key.getAttribute('type'), 'password');
      assert.equal(await page.getByLabel('Current balance (USD)', { exact: true }).inputValue(), '');
      assert.equal(await page.getByRole('button', { name: 'Save access', exact: true }).isDisabled(), true);
      assert.equal(await page.getByRole('button', { name: 'Disconnect balance', exact: true }).isDisabled(), true);
      assert.equal(await usage.getByRole('button', { name: 'Save', exact: true }).isDisabled(), true);
      assert.equal(await usage.getByRole('button', { name: 'Clear', exact: true }).isDisabled(), true);
      assert.equal(await page.getByRole('button', { name: 'Sign in to OpenAI Platform', exact: true }).isVisible(), true);
      assert.equal(await page.getByRole('dialog').count(), 0);
      // The fixture records attempts even when a socket is closed or a panel-start
      // fetch is intercepted before it reaches Playwright's network routing.
      assert.deepEqual(await page.evaluate(() => ({
        sockets: window.browserFixture.socketCount,
        openSockets: window.browserFixture.openSockets,
        loginOpened: window.browserFixture.loginOpened,
        loginSocketUrl: window.browserFixture.loginSocketUrl,
        starts: window.browserFixture.starts,
        messages: window.browserFixture.messages,
      })), { sockets: 0, openSockets: 0, loginOpened: false, loginSocketUrl: '', starts: [], messages: [] });
      assert.equal(page.context().pages().length, 1, 'no login/billing browser opened');
      assert.equal(page.url(), url.href, 'no automatic billing navigation');
      assert.deepEqual([...calls].sort((a, b) => a.path.localeCompare(b.path)), [
        { path: '/api/v1/model-providers/fixture-openai/balance-access', method: 'GET' },
        { path: '/api/v1/model-providers/fixture-openai/openai-usage', method: 'GET' },
      ], 'no background reads, credential writes or disconnects');
      assert.deepEqual(unexpected, []);
      assert.deepEqual(errors, []);
    };
    await assertPassive();
    // Advance timers, not wall-clock sleeps: mounting must not schedule hidden polling.
    await page.clock.fastForward(60_000);
    await assertPassive();
    assert.equal(await usage.evaluate(el => el.scrollWidth > el.clientWidth), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight), false);
    const screenshot = join(tmpdir(), `provider-login-${viewport.width}.png`);
    await page.screenshot({ path: screenshot });
    console.log(JSON.stringify({ providerUsageGuidance: true, viewport,
      unsupportedPrepaidBalance: true, organizationSpendSeparate: true, chatgptCreditsSeparate: true,
      writeOnlyAccessInput: true, manualBalanceFallback: true, apiCalls: calls, automaticPolling: false,
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
            void fetch('/api/v1/model-providers/p1/openai-usage').catch(() => {});
          }, 30_000);
          return beforeMount;
        }), true, 'the polling canary must be scheduled before component mount');
        return route.continue();
      });
      return page;
    },
  };
  await assert.rejects(verifyProviderLogin(pollingBrowser, fixtureUrl, viewport), {
    code: 'ERR_ASSERTION', message: /no background reads, credential writes or disconnects/,
  }, 'the verifier must reject polling scheduled before component mount');
  const polling = requests.filter(request => request.path === '/api/v1/model-providers/p1/openai-usage');
  assert.ok(polling.length > 0, 'the polling canary must actually attempt its extra request');
  for (const request of polling) {
    assert.deepEqual(request, { path: '/api/v1/model-providers/p1/openai-usage', method: 'GET' });
  }
  console.log(JSON.stringify({ providerPollingCanary: true, viewport, detectedRequests: requests }));
}
