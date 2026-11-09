// Settings > Memory against a mocked memories API, so the page's writes can be
// exercised on the preview cookie: filter, search, save, a stale save keeps the
// draft, a refused write stays readable, delete asks first, phone Back is clean.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const mobileShell = process.argv.includes('--mobile-shell');
const STALE = 'This memory changed since you opened it. Refresh it and try again.';
const memory = (id, text, extra = {}) => ({ id, revision: 1, metadata_version: 'meta-1', text, when: null, status: 'active', tier: 'agent', source: 'tool', scope: 'global', scope_label: 'All projects', always: false, needs_review: false, this_worker_only: false, created_at: 1.77e9, updated_at: 1.77e9, last_recalled_at: null, expires_at: null, uses: 0, ...extra });
const seed = () => [
 memory('a', 'The nightly build publishes at two in the morning.', { when: 'When checking the nightly build', expires_at: 1.9e9 }),
 memory('b', 'The greenhouse controller reports humidity every five minutes.', { tier: 'learned' }),
 memory('c', 'The old toolchain was retired in the spring.', { status: 'archived' }),
 memory('d', 'Two facts disagree about the staging address.', { needs_review: true }),
];
const workspace = page => page.getByRole('navigation', { name: 'Workspace', exact: true });
const rows = page => page.locator('.mem-list .st-it').filter({ visible: true });
const text = page => page.getByRole('textbox', { name: 'What to remember', exact: true }).filter({ visible: true });
const button = (page, name) => page.getByRole('button', { name, exact: true }).filter({ visible: true }).first();
const showing = (page, count) => page.waitForFunction(n => [...document.querySelectorAll('.mem-list .st-it')].filter(el => el.checkVisibility()).length === n, count);
const formOffset = page => text(page).evaluate(el => el.getBoundingClientRect().top - el.closest('.st-dwrap').getBoundingClientRect().top);
const saved = page => page.waitForFunction(() => [...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'Save' && b.disabled && b.checkVisibility()));
// The service worker would answer the memories request before page.route sees it.
await runJourney({ name: 'settings-memory', browser: async () => ({ context: { serviceWorkers: 'block' } }), allow: DEV_INSTANCE_NOISE, cases: [{
 name: mobileShell ? 'native-see-edit-delete' : 'see-edit-delete', viewports: mobileShell ? ['phone'] : ['desktop', 'phone'], throttles: ['fast'],
 allow: [
  { kind: 'failed_requests', status: 409, url: /\/memories\/a(\?|$)/, reason: 'this case answers a stale save on purpose' },
  { kind: 'failed_requests', status: 403, url: /\/memories\/a(\?|$)/, reason: 'this case refuses a write on purpose' },
  { kind: 'failed_requests', status: 403, url: /\/terminals/, reason: 'the preview cookie may not use terminals (require_terminal_owner refuses preview access by design)' },
 ],
 async setup({ page, status, variant }) {
  const api = page.memoryApi = { items: seed(), mode: 'ok', puts: [], deletes: [] };
  await page.route(/\/api\/v1\/memories(\/[^/?]+)?(\?.*)?$/, async route => {
   const request = route.request(), url = new URL(request.url()), id = url.pathname.split('/memories/')[1];
   if (request.method() === 'GET') return route.fulfill({ json: { items: api.items, limits: { text_chars: 2000, when_chars: 140 } } });
   if (request.method() === 'DELETE') { api.deletes.push(url.search); api.items = api.items.filter(m => m.id !== id); return route.fulfill({ status: 204, body: '' }); }
   const body = request.postDataJSON(); api.puts.push(body);
   if (api.mode === 'refused') return route.fulfill({ status: 403, json: { detail: 'Forbidden' } });
   const current = api.items.find(m => m.id === id);
   if (api.mode === 'stale') { current.text = 'Changed on another device.'; current.revision += 1; return route.fulfill({ status: 409, json: { detail: STALE } }); }
   Object.assign(current, { text: body.text, when: body.when, tier: 'user', revision: current.revision + 1 });
   return route.fulfill({ json: current });
  });
  if (mobileShell) {
   await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
   expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
   await page.getByRole('button', { name: /^Open menu(?: \(\d+ unread notifications\))?$/ }).click();
  } else {
   await gotoApp(page, status);
   if (variant.viewport === 'phone') await showSidebar(page);
  }
  await workspace(page).getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'Memory', exact: true }).filter({ visible: true }).first().click();
  await rows(page).first().waitFor({ timeout: 15000 });
 },
 async act({ page, mark, variant }) {
  const api = page.memoryApi, phone = variant.viewport === 'phone';
  await mark('active memories list by default and search narrows them');
  await showing(page, 3);
  const search = page.getByPlaceholder('Search memories').filter({ visible: true });
  await search.fill('checking the nightly');
  await showing(page, 1);
  await search.fill('');
  await showing(page, 3);
  await mark('archived memories sit behind the filter');
  await page.locator('.mem-filters select').filter({ visible: true }).first().selectOption('archived');
  await page.getByText('The old toolchain was retired').filter({ visible: true }).first().waitFor();
  await page.locator('.mem-filters select').filter({ visible: true }).first().selectOption('active');
  await showing(page, 3);
  await mark('an edit saves only on Save, against the version it opened');
  await rows(page).filter({ hasText: 'nightly' }).click();
  await text(page).waitFor();
  expect(await button(page, 'Save').isDisabled(), 'nothing to save before an edit');
  await text(page).fill('The nightly build publishes at three in the morning.');
  const offset = await formOffset(page);
  await button(page, 'Save').click();
  await saved(page);
  expect(Math.abs(await formOffset(page) - offset) < 1, 'saving an agent memory does not move the form');
  expect(api.puts.length === 1 && api.puts[0].expected_revision === 1 && api.puts[0].expected_metadata_version === 'meta-1' && api.puts[0].when === 'When checking the nightly build', 'save sends the opened version and keeps the when-line ' + JSON.stringify(api.puts));
  await mark('a stale save keeps the draft and offers the latest version');
  api.mode = 'stale';
  const draft = 'A draft the owner is still typing.';
  await text(page).fill(draft);
  await button(page, 'Save').click();
  await page.getByRole('alert').filter({ hasText: /still here/ }).filter({ visible: true }).waitFor();
  expect(await text(page).inputValue() === draft, 'a stale save must not touch the draft');
  api.mode = 'ok';
  await button(page, 'Load latest').click();
  await page.waitForFunction(() => [...document.querySelectorAll('textarea.mem-text')].some(t => t.value === 'Changed on another device.'));
  await mark('a refused write reads as a sentence and keeps the draft');
  api.mode = 'refused';
  await text(page).fill(draft);
  await button(page, 'Save').click();
  const refused = page.getByRole('alert').filter({ visible: true }).first();
  await refused.waitFor();
  const message = await refused.innerText();
  expect(message.length > 12 && !/undefined|\[object|^Forbidden$/.test(message), 'refusal is readable: ' + message);
  expect(await text(page).inputValue() === draft, 'a refused write must not touch the draft');
  api.mode = 'ok';
  await button(page, 'Discard').click();
  await mark('delete asks first, then removes the memory');
  await button(page, 'Delete').click();
  await page.getByText(/permanently on all your workers/).filter({ visible: true }).first().waitFor();
  expect(api.deletes.length === 0, 'nothing is deleted before the confirm');
  await page.locator('.mem-detail button', { hasText: /^Delete$/ }).filter({ visible: true }).last().click();
  await page.waitForFunction(() => ![...document.querySelectorAll('textarea.mem-text')].some(t => t.checkVisibility()));
  expect(api.deletes.length === 1 && api.deletes[0].includes('expected_revision=') && api.deletes[0].includes('expected_metadata_version=meta-1'), 'delete carries the opened version ' + JSON.stringify(api.deletes));
  await showing(page, 2);
  if (phone) {
   await page.goBack();
   await page.waitForTimeout(300);
   expect(await text(page).count() === 0, 'Back never reopens a deleted memory');
  }
 },
}] });
