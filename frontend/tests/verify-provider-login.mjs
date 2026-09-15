import assert from 'node:assert/strict';

// Real Admin usage component; HTTP responses are fixtures, never live account data.
export async function verifyProviderLogin(browser, fixtureUrl, viewport) {
  const page = await browser.newPage({ viewport });
  const errors = [], unexpected = [], calls = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  const initial = { configured: false, organization_id: null, spend: null,
    fetched_at: null, last_error_kind: null, source: 'OpenAI organization Admin API', scope: 'Organization' };
  let status = initial;
  await page.route('**/*', async route => {
    const request = route.request(), target = new URL(request.url());
    if (target.pathname.startsWith('/api/')) {
      calls.push({ path: target.pathname, method: request.method(), body: request.postDataJSON() });
      if (target.pathname !== '/api/v1/model-providers/p1/openai-usage' ||
          !['GET', 'PUT', 'DELETE'].includes(request.method())) {
        unexpected.push(request.url()); return route.abort();
      }
      if (request.method() === 'PUT') {
        assert.deepEqual(request.postDataJSON(), { admin_key: 'fixture-admin-key', organization_id: 'org-fixture' });
        status = { ...initial, configured: true, organization_id: 'org-fixture' };
      }
      if (request.method() === 'DELETE') status = initial;
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
    const form = page.getByRole('region', { name: 'OpenAI account usage' });
    await form.getByText('Admin usage access is not configured.', { exact: false }).waitFor();
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Sign in for credits', exact: true }).count(), 0);
    assert.equal(await page.evaluate(() => window.browserFixture.openSockets), 0);
    assert.equal(await form.getByLabel('Admin API key', { exact: true }).getAttribute('type'), 'password');
    assert.ok((await form.innerText()).includes('does not retrieve your prepaid credit balance'));

    // No polling timer should restart billing or read status behind the user's back.
    await page.clock.install();
    const initialReads = calls.length;
    await page.clock.fastForward(60_000);
    assert.equal(calls.length, initialReads);
    const organization = form.getByLabel('Organization ID', { exact: true });
    await organization.fill('not-an-organization');
    assert.equal(await organization.evaluate(el => el.checkValidity()), false);
    await organization.fill('org-fixture_a-1');
    assert.equal(await organization.evaluate(el => el.checkValidity()), true);
    await organization.fill('org-fixture');
    await form.getByLabel('Admin API key', { exact: true }).fill('fixture-admin-key');
    await form.getByRole('button', { name: 'Save admin key', exact: true }).click();
    await form.getByRole('status').waitFor();
    assert.equal(await form.getByLabel('Admin API key', { exact: true }).inputValue(), '');
    assert.equal(await page.evaluate(() => window.browserFixture.openSockets), 0);
    assert.equal(await page.getByRole('dialog').count(), 0);

    status = { ...status, fetched_at: 1_700_000_000_000,
      spend: { spend_month: 12.5, input_tokens: 1200, output_tokens: 400, num_model_requests: 6 },
      last_error_kind: 'billing_access_denied' };
    await form.getByRole('button', { name: 'Read cached status', exact: true }).click();
    await form.getByText('$12.50 organization spend this month (UTC)', { exact: true }).waitFor();
    assert.ok((await form.innerText()).includes('Polling is paused for this key'));
    const readsAfterRefresh = calls.length;
    await page.clock.fastForward(60_000);
    assert.equal(calls.length, readsAfterRefresh);
    assert.equal(await form.evaluate(el => el.scrollWidth > el.clientWidth), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const screenshot = `/tmp/provider-login-${viewport.width}.png`;
    await page.screenshot({ path: screenshot });

    await form.getByRole('button', { name: 'Disconnect usage', exact: true }).click();
    await form.getByRole('button', { name: 'Cancel', exact: true }).click();
    assert.equal(calls.filter(call => call.method === 'DELETE').length, 0);
    await form.getByRole('button', { name: 'Disconnect usage', exact: true }).click();
    await form.getByRole('button', { name: 'Remove admin key', exact: true }).click();
    await form.getByText('Admin usage access is not configured.', { exact: false }).waitFor();
    assert.equal(calls.filter(call => call.method === 'DELETE').length, 1);
    assert.equal(await form.getByLabel('Organization ID', { exact: true }).inputValue(), '');
    assert.equal(await page.evaluate(() => window.browserFixture.openSockets), 0);
    assert.deepEqual(unexpected, []);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ providerAdminUsage: true, viewport, separateCredential: true,
      passwordCleared: true, explicitDisconnect: true, automaticPolling: false,
      loginBrowser: false, overflow: false, pageErrors: errors, screenshot }));
  } finally { await page.close(); }
}
