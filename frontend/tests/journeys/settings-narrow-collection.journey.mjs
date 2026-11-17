// Settings collections in a window too narrow for list and detail side by side
// (#1406): the open item replaces the list and nothing spills past the pane,
// Back returns to the list with focus on the row, an item that no longer exists
// can still be left, and Logs "Same logger" shows its filtered list. A guard at
// 1280 keeps the split unchanged. On a phone (#1582) a detail opens at its top,
// Back returns to the list's scroll position, and reopening the same item
// starts its pane at the top. API users, logs and hooks are route stubs, so the
// worker's real data never decides the result.
import { runJourney } from './runner.mjs';
import { SEEDED, expect, gotoApp, openFromSidebar, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const user = { id: 'u1', name: 'Build bot', description: '', project_id: null, profile_id: null, model: null, effort: null, permission_mode: null, allowed_agents: [], is_active: true, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z', last_used_at: null };
const line = (minute, level, logger_name, message) => ({ timestamp: `2026-10-01T14:${minute}:00.000Z`, level, logger_name, message });
const LOGS = [
 line('45', 'INFO', 'app.journey.alpha', 'Alpha started'),
 line('44', 'WARNING', 'app.journey.beta', 'Beta is slow'),
 line('43', 'INFO', 'app.journey.alpha', 'Alpha ready'),
 line('42', 'ERROR', 'app.journey.gamma', 'Gamma failed'),
];

const coll = page => page.locator('.settings-shell .st-coll').filter({ visible: true }).first();
const settingsNav = page => page.getByRole('navigation', { name: 'Settings', exact: true });
const button = (page, name) => page.getByRole('button', { name, exact: true }).filter({ visible: true }).first();
const replaceState = page => coll(page).evaluate(el => el.dataset.replace ?? null);
const listShown = page => coll(page).evaluate(el => !!el.querySelector('.st-lcol')?.checkVisibility());
const focusedItem = page => page.evaluate(() => document.activeElement?.closest('[data-settings-item]')?.getAttribute('data-settings-item') ?? null);
const focusInList = page => page.evaluate(() => !!document.activeElement?.closest('.st-lcol'));
const visibleLogRows = page => page.evaluate(() => [...document.querySelectorAll('.st-lcol [data-settings-item]')].filter(el => el.checkVisibility()).length);

// Every visible element of the open detail stays inside the pane and the window.
async function spill(page) {
 return coll(page).evaluate(el => {
  const pane = el.querySelector('.st-dpane').getBoundingClientRect();
  const out = [];
  for (const node of el.querySelectorAll('.st-dpane *')) {
   if (!node.checkVisibility()) continue;
   const r = node.getBoundingClientRect();
   if (!r.width) continue;
   if (r.right > Math.min(pane.right, innerWidth) + 1 || r.left < pane.left - 1) out.push(`${node.tagName.toLowerCase()}.${[...node.classList].join('.')} ${Math.round(r.left)}..${Math.round(r.right)}`);
  }
  return { out: out.slice(0, 5), page: document.documentElement.scrollWidth > innerWidth };
 });
}

async function stub(page, { users = [], lines = LOGS } = {}) {
 await page.route(/\/api\/v1\/api-users(\?.*)?$/, route => route.fulfill({ json: users }));
 await page.route(/\/api\/v1\/logs\?/, route => route.fulfill({ json: { lines, total: lines.length, returned: lines.length, levels: ['INFO', 'WARNING', 'ERROR'] } }));
}

async function openSettings(page, status, width) {
 await page.setViewportSize({ width, height: 800 });
 await gotoApp(page, status);
 const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
 await nav.getByRole('button', { name: 'Settings', exact: true }).click();
 await settingsNav(page).waitFor();
}

async function category(page, name) {
 await settingsNav(page).getByRole('button', { name, exact: true }).filter({ visible: true }).first().click();
 await page.getByRole('heading', { name, exact: true, level: 1 }).filter({ visible: true }).first().waitFor();
}

const entryKey = page => page.evaluate(() => navigation.currentEntry.key);

async function sameLogger(page) {
 await category(page, 'Logs');
 await page.locator('.st-lcol [data-settings-item]').filter({ visible: true }).first().waitFor();
 expect(await visibleLogRows(page) === 4, 'Logs lists the four stubbed lines');
 const listEntry = await entryKey(page);
 await page.locator('.st-lcol [data-settings-item]').filter({ hasText: 'Alpha started' }).first().click();
 await button(page, 'Same logger').waitFor();
 const detailEntry = await entryKey(page);
 expect(detailEntry !== listEntry, 'opening a log entry pushes its detail');
 await button(page, 'Same logger').click();
 await page.waitForFunction(() => [...document.querySelectorAll('.st-lcol [data-settings-item]')].filter(el => el.checkVisibility()).length === 2, null, { timeout: 5000 });
 return { listEntry, detailEntry };
}

// App hooks for the phone scroll case: enough rows that the list scrolls.
const HOOKS = Array.from({ length: 20 }, (_, i) => ({ id: `hk${i + 1}`, project_id: null, name: `Check hook ${i + 1}`, event: 'PreToolUse', hook_type: 'command', command: `echo check ${i + 1}`, timeout: 30, url: null, has_secret: false, enabled: true, tool_matcher: null, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z', last_triggered_at: null, failure_count: 0 }));
const scrollState = page => page.evaluate(() => ({ page: Math.round(document.querySelector('.settings-viewport')?.scrollTop ?? -1), pane: Math.round(document.querySelector('.settings-shell .st-dpane')?.scrollTop ?? -1) }));
async function waitScroll(page, want) {
 await page.waitForFunction(w => Object.entries(w).every(([k, v]) => Math.abs(Math.round((k === 'page' ? document.querySelector('.settings-viewport') : document.querySelector('.settings-shell .st-dpane'))?.scrollTop ?? -99) - v) <= 1), want, { timeout: 5000 }).catch(() => {});
 return scrollState(page);
}
const hookRow = page => page.locator('.st-lcol [data-settings-item]').filter({ hasText: 'Check hook 12' }).filter({ visible: true }).first();
const hookTitle = page => page.locator('[data-settings-heading="detail"]').filter({ hasText: 'Check hook 12' }).filter({ visible: true }).first();
const stubHooks = page => page.route(/\/api\/v1\/hooks\/app(\?.*)?$/, route => (route.request().method() === 'GET' ? route.fulfill({ json: HOOKS }) : route.fallback()));
async function phoneHooks(page, status) {
 await stubHooks(page);
 await gotoApp(page, status);
 await (await showSidebar(page)).getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
 await settingsNav(page).waitFor();
 await category(page, 'Hooks');
 await hookRow(page).waitFor();
}
// Stacked Settings has one scroller, .settings-viewport (#1607): every other
// element inside it that could scroll vertically and has more to show.
const innerScrollers = page => page.evaluate(() => {
 const view = document.querySelector('.settings-viewport');
 return [...view.querySelectorAll('*')].filter(el => {
  if (!el.checkVisibility() || el.matches('textarea, select, input')) return false;
  const y = getComputedStyle(el).overflowY;
  return (y === 'auto' || y === 'scroll') && el.scrollHeight > el.clientHeight + 1;
 }).map(el => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} ${el.clientHeight}/${el.scrollHeight}`);
});
// Where a page heading sits against the top bar, and the scroller's offset.
async function headingPlace(page, name, level) {
 const heading = page.getByRole('heading', { name, exact: true, level }).filter({ visible: true }).first();
 await heading.waitFor();
 const top = await heading.evaluate(el => el.getBoundingClientRect().top);
 const bar = await page.getByRole('banner').filter({ visible: true }).first().evaluate(el => el.getBoundingClientRect().bottom);
 return { top: Math.round(top), bar: Math.round(bar), page: Math.round(await page.evaluate(() => document.querySelector('.settings-viewport').scrollTop)) };
}

// The service worker would answer the stubbed requests before page.route sees them.
await runJourney({ name: 'settings-narrow-collection', browser: async () => ({ context: { serviceWorkers: 'block' } }), allow: DEV_INSTANCE_NOISE, cases: [
 {
  name: 'one-pane-run-and-back', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stub(page);
   await openSettings(page, status, 800);
   await category(page, 'Integrations');
  },
  async act({ page, mark }) {
   await mark('Add API user, Run tab: the detail replaces the list');
   await button(page, 'Add API user').click();
   await page.getByRole('tab', { name: 'Run', exact: true }).filter({ visible: true }).first().click();
   await page.getByRole('tabpanel', { name: 'Run' }).filter({ visible: true }).first().waitFor();
   expect(await replaceState(page) === 'detail', `one pane at 800 wide (data-replace=${await replaceState(page)})`);
   expect(!(await listShown(page)), 'the list is hidden while the item is open');
   const s = await spill(page);
   expect(s.out.length === 0 && !s.page, `nothing in the detail runs past the pane or the window: ${s.out.join('; ')}`);

   await mark('Back returns to the list with focus on the row');
   await button(page, 'Back to Integrations').click();
   await page.waitForFunction(() => !!document.querySelector('.st-lcol')?.checkVisibility());
   expect(await focusedItem(page) === 'new-api-user', `focus is on the draft's row (${await focusedItem(page)})`);
  }
 },
 {
  name: 'not-found-has-a-way-back', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stub(page);
   // A remembered item that no longer exists, as after deleting it elsewhere.
   await page.addInitScript(() => {
    if (sessionStorage.getItem('journey.seeded')) return;
    sessionStorage.setItem('journey.seeded', '1');
    sessionStorage.setItem('settings.nav', JSON.stringify({ category: 'integrations', view: 'detail', selected: { integrations: 'u-gone' } }));
   });
   await openSettings(page, status, 800);
  },
  async act({ page, mark }) {
   await mark('Not found replaces the list and offers Back');
   await page.getByRole('heading', { name: 'Not found', exact: true }).filter({ visible: true }).first().waitFor();
   expect(await replaceState(page) === 'detail' && !(await listShown(page)), 'the not-found item fills the page');
   const back = button(page, 'Back to Integrations');
   expect(await back.isVisible(), 'a Back to the list is offered');
   await back.focus();
   await page.keyboard.press('Enter');
   await page.waitForFunction(() => !!document.querySelector('.st-lcol')?.checkVisibility());
   expect(await focusInList(page), 'focus lands in the list, not on the page body');
   expect(!(await page.getByRole('heading', { name: 'Not found', exact: true }).filter({ visible: true }).count()), 'the not-found state is gone');
  }
 },
 {
  name: 'same-logger-shows-the-list', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stub(page);
   await openSettings(page, status, 800);
  },
  async act({ page, mark }) {
   await mark('Same logger closes the entry and shows the filtered list');
   const previousEntry = await entryKey(page);
   const { listEntry } = await sameLogger(page);
   expect(await listShown(page), 'the filtered list is on screen');
   expect(await visibleLogRows(page) === 2, `two alpha lines listed (${await visibleLogRows(page)})`);
   expect(await replaceState(page) !== 'detail', 'no entry hides the list');
   expect(await focusInList(page), 'focus is in the list');
   expect(await entryKey(page) === listEntry, 'Same logger returns to the existing Logs list entry');
   await mark('browser Back leaves Logs');
   await page.goBack();
   await page.getByRole('heading', { name: 'Logs', exact: true, level: 1 }).waitFor({ state: 'hidden' });
   expect(await entryKey(page) === previousEntry, 'Back reaches the entry before Logs in one step');
  }
 },
 {
  // #1582: on a phone the detail replaces the list in one scroller. It opens at
  // its top with the title below the top bar, Back returns to where the list
  // was, and reopening the same item starts its own scrolled pane at the top.
  name: 'phone-detail-opens-at-top', viewports: ['phone'], throttles: ['fast'],
  async setup({ page, status }) {
   await page.route(/\/api\/v1\/hooks\/app(\?.*)?$/, route => (route.request().method() === 'GET' ? route.fulfill({ json: HOOKS }) : route.fallback()));
   await gotoApp(page, status);
   await (await showSidebar(page)).getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await settingsNav(page).waitFor();
   await category(page, 'Hooks');
   await hookRow(page).waitFor();
  },
  async act({ page, mark }) {
   await mark('scroll the list, open a hook');
   await page.evaluate(() => document.querySelector('.settings-viewport').scrollTo({ top: 250 }));
   const listTop = (await waitScroll(page, { page: 250 })).page;
   expect(listTop > 100, `the list scrolls (${listTop})`);
   await hookRow(page).click();
   await hookTitle(page).waitFor();
   let s = await waitScroll(page, { page: 0 });
   const titleTop = await hookTitle(page).evaluate(el => el.getBoundingClientRect().top);
   const barBottom = await page.getByRole('banner').filter({ visible: true }).first().evaluate(el => el.getBoundingClientRect().bottom);
   expect(s.page === 0, `the detail opens at its top (${s.page})`);
   expect(titleTop >= barBottom, `the title is below the top bar (title ${Math.round(titleTop)}, bar ${Math.round(barBottom)})`);

   await mark('scroll the detail, Back returns to the list where it was');
   await page.evaluate(() => { document.querySelector('.settings-viewport').scrollTo({ top: 200 }); document.querySelector('.settings-shell .st-dpane').scrollTo({ top: 200 }); });
   const read = await scrollState(page);
   expect(read.page > 0 || read.pane > 0, `the detail scrolls (${JSON.stringify(read)})`);
   await button(page, 'Back to Hooks').click();
   await hookRow(page).waitFor();
   s = await waitScroll(page, { page: listTop });
   expect(Math.abs(s.page - listTop) <= 1, `Back returns to the list at ${listTop} (${s.page})`);

   await mark('reopen the same hook: page and pane start at the top');
   await hookRow(page).click();
   await hookTitle(page).waitFor();
   s = await waitScroll(page, { page: 0, pane: 0 });
   expect(s.page === 0 && s.pane === 0, `the same hook reopens at its top (${JSON.stringify(s)})`);
   await button(page, 'Back to Hooks').click();
   await hookRow(page).waitFor();
   s = await waitScroll(page, { page: listTop });
   expect(Math.abs(s.page - listTop) <= 1, `Back returns to the list at ${listTop} again (${s.page})`);
  }
 },
 {
  // Live QA on #1593: turned sideways the phone shows one pane at a time, so
  // a hook opened there must still start at its top once the phone is
  // upright again. Back after the turn shows the row it came from, though the
  // list was laid out again.
  name: 'phone-turn-opens-at-top', viewports: ['phone'], throttles: ['fast'],
  async setup({ page, status }) {
   await page.route(/\/api\/v1\/hooks\/app(\?.*)?$/, route => (route.request().method() === 'GET' ? route.fulfill({ json: HOOKS }) : route.fallback()));
   await gotoApp(page, status);
   await (await showSidebar(page)).getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await settingsNav(page).waitFor();
   await category(page, 'Hooks');
   await hookRow(page).waitFor();
  },
  async act({ page, mark }) {
   const row = name => page.locator('.st-lcol [data-settings-item]').filter({ hasText: new RegExp(`^\\s*${name}\\b`) }).filter({ visible: true }).first();
   const title = name => page.locator('[data-settings-heading="detail"]').filter({ hasText: name }).filter({ visible: true }).first();
   // Inside the settings scroller's visible box (no top bar shows sideways).
   const onScreen = name => row(name).evaluate(el => {
    const v = document.querySelector('.settings-viewport').getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return { top: Math.round(r.top), bottom: Math.round(r.bottom), view: [Math.round(v.top), Math.round(v.bottom)], ok: r.top >= v.top - 1 && r.bottom <= v.bottom + 1 };
   });
   await mark('scroll the list, open a hook, scroll it');
   await page.evaluate(() => document.querySelector('.settings-viewport').scrollTo({ top: 250 }));
   await waitScroll(page, { page: 250 });
   await row('Check hook 12').click();
   await title('Check hook 12').waitFor();
   // Stacked, the detail scrolls in the page scroller, not in its pane (#1607).
   await page.evaluate(() => { document.querySelector('.settings-viewport').scrollTo({ top: 300 }); document.querySelector('.settings-shell .st-dpane').scrollTo({ top: 300 }); });
   const read = await waitScroll(page, { page: 300 });
   expect(read.page > 100 && read.pane === 0, `the detail scrolls in the page only (${JSON.stringify(read)})`);

   await mark('turn sideways, Back shows the row it came from');
   await page.setViewportSize({ width: 844, height: 390 });
   await page.waitForFunction(() => document.querySelector('.settings-shell .st-coll')?.dataset.replace === 'detail', null, { timeout: 5000 });
   await button(page, 'Back to Hooks').click();
   await row('Check hook 12').waitFor();
   await page.waitForTimeout(300);
   const back = await onScreen('Check hook 12');
   expect(back.ok, `Back shows the hook's row on screen (${JSON.stringify(back)})`);

   await mark('open another hook sideways');
   await row('Check hook 5').click();
   await title('Check hook 5').waitFor();

   await mark('upright again: the new hook starts at its top');
   await page.setViewportSize({ width: 390, height: 844 });
   await page.waitForFunction(() => { const c = document.querySelector('.settings-shell .st-coll'); return !!c?.closest('.settings-shell')?.hasAttribute('data-stacked') && !c.dataset.replace; }, null, { timeout: 5000 });
   await page.waitForTimeout(300);
   const s = await scrollState(page);
   const titleTop = await title('Check hook 5').evaluate(el => el.getBoundingClientRect().top);
   const barBottom = await page.getByRole('banner').filter({ visible: true }).first().evaluate(el => el.getBoundingClientRect().bottom);
   expect(s.page === 0 && s.pane === 0, `the hook opened sideways starts at its top upright (${JSON.stringify(s)})`);
   expect(titleTop >= barBottom, `its title is below the top bar (title ${Math.round(titleTop)}, bar ${Math.round(barBottom)})`);
  }
 },
 {
  // #1608: the stacked categories share one scroller, so a category must open
  // at its top rather than at the offset the previous one was left at.
  name: 'phone-category-opens-at-top', viewports: ['phone'], throttles: ['fast'],
  async setup({ page, status }) { await phoneHooks(page, status); },
  async act({ page, mark }) {
   await mark('scroll the Hooks list to its end, go back to the index');
   await page.evaluate(() => { const v = document.querySelector('.settings-viewport'); v.scrollTo({ top: v.scrollHeight }); });
   await page.waitForTimeout(200);
   const end = await scrollState(page);
   expect(end.page > 200, `the Hooks list scrolls (${end.page})`);
   await button(page, 'Back to Settings').click();
   await settingsNav(page).getByRole('button', { name: 'General', exact: true }).filter({ visible: true }).first().waitFor();

   await mark('open General: it starts at its top');
   await category(page, 'General');
   await page.waitForTimeout(300);
   let at = await headingPlace(page, 'General', 1);
   expect(at.page === 0 && at.top >= at.bar, `General opens at its top (${JSON.stringify(at)})`);

   await mark('scroll General, back, reopen it: still at its top');
   await page.evaluate(() => { const v = document.querySelector('.settings-viewport'); v.scrollTo({ top: v.scrollHeight }); });
   await page.waitForTimeout(200);
   expect((await scrollState(page)).page > 100, 'General scrolls');
   await button(page, 'Back to Settings').click();
   await category(page, 'General');
   await page.waitForTimeout(300);
   at = await headingPlace(page, 'General', 1);
   expect(at.page === 0 && at.top >= at.bar, `General reopens at its top (${JSON.stringify(at)})`);
  }
 },
 {
  // #1607: a stacked Hooks detail scrolls in the page scroller only; the pane
  // must not become a second scroller nested inside it.
  name: 'phone-detail-one-scroller', viewports: ['phone'], throttles: ['fast'],
  async setup({ page, status }) { await phoneHooks(page, status); },
  async act({ page, mark }) {
   await mark('open a hook: one scroller');
   await hookRow(page).click();
   await hookTitle(page).waitFor();
   await page.waitForTimeout(300);
   const inner = await innerScrollers(page);
   expect(inner.length === 0, `nothing inside the settings scroller scrolls on its own: ${inner.join('; ')}`);
   const pane = await page.evaluate(() => {
    const p = document.querySelector('.settings-viewport .st-dpane').getBoundingClientRect();
    const v = document.querySelector('.settings-viewport').getBoundingClientRect();
    return { overflow: getComputedStyle(document.querySelector('.settings-viewport .st-dpane')).overflowY, pane: Math.round(p.height), view: Math.round(v.height) };
   });
   expect(pane.overflow === 'visible', `the stacked pane does not scroll on its own (${JSON.stringify(pane)})`);
  }
 },
 {
  // #1612: Settings hidden behind a chat while the phone turns upright. A
  // hook opened sideways still starts at its top when Settings comes back.
  name: 'phone-hidden-turn-opens-at-top', viewports: ['phone'], throttles: ['fast'],
  async setup({ page, status }) { await phoneHooks(page, status); },
  async act({ page, mark }) {
   const row = name => page.locator('.st-lcol [data-settings-item]').filter({ hasText: new RegExp(`^\\s*${name}\\b`) }).filter({ visible: true }).first();
   const title = name => page.locator('[data-settings-heading="detail"]').filter({ hasText: name }).filter({ visible: true }).first();
   await mark('open hook 12 upright and scroll it');
   await row('Check hook 12').click();
   await title('Check hook 12').waitFor();
   await page.evaluate(() => { document.querySelector('.settings-viewport').scrollTo({ top: 300 }); document.querySelector('.settings-shell .st-dpane').scrollTo({ top: 300 }); });
   await page.waitForTimeout(200);

   await mark('turn sideways, Back, open hook 5');
   await page.setViewportSize({ width: 844, height: 390 });
   await page.waitForFunction(() => document.querySelector('.settings-shell .st-coll')?.dataset.replace === 'detail', null, { timeout: 5000 });
   await button(page, 'Back to Hooks').click();
   await row('Check hook 5').waitFor();
   await row('Check hook 5').click();
   await title('Check hook 5').waitFor();

   await mark('switch to a chat, turn upright');
   await openFromSidebar(page, SEEDED.no_project);
   await page.waitForFunction(() => !document.querySelector('.settings-shell')?.checkVisibility(), null, { timeout: 5000 });
   await page.setViewportSize({ width: 390, height: 844 });
   await page.waitForTimeout(500);
   await mark('come back to Settings');
   await (await showSidebar(page)).getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await title('Check hook 5').waitFor();
   await page.waitForTimeout(300);
   const s = await scrollState(page);
   const titleTop = await title('Check hook 5').evaluate(el => el.getBoundingClientRect().top);
   const barBottom = await page.getByRole('banner').filter({ visible: true }).first().evaluate(el => el.getBoundingClientRect().bottom);
   expect(s.page === 0 && s.pane === 0, `hook 5 is at its top after the hidden turn (${JSON.stringify(s)})`);
   expect(titleTop >= barBottom, `its title is below the top bar (title ${Math.round(titleTop)}, bar ${Math.round(barBottom)})`);
  }
 },
 {
  // #1612, split variant: the open pane scrolls on its own beside the list. A
  // hook opened one pane at a time, then Settings hidden while the window
  // widens, still starts at its top when Settings comes back split.
  name: 'split-hidden-widen-opens-at-top', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stubHooks(page);
   await page.setViewportSize({ width: 1280, height: 500 });
   await gotoApp(page, status);
   await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await settingsNav(page).waitFor();
   await category(page, 'Hooks');
   await hookRow(page).waitFor();
  },
  async act({ page, mark }) {
   const row = name => page.locator('.st-lcol [data-settings-item]').filter({ hasText: new RegExp(`^\\s*${name}\\b`) }).filter({ visible: true }).first();
   const title = name => page.locator('[data-settings-heading="detail"]').filter({ hasText: name }).filter({ visible: true }).first();
   const pane = () => page.evaluate(() => Math.round(document.querySelector('.settings-shell .st-dpane').scrollTop));
   await mark('open hook 12 beside the list and scroll its pane');
   await row('Check hook 12').click();
   await title('Check hook 12').waitFor();
   await page.evaluate(() => document.querySelector('.settings-shell .st-dpane').scrollTo({ top: 300 }));
   await page.waitForTimeout(200);
   expect(await pane() > 100, `the pane scrolls on its own (${await pane()})`);

   await mark('narrow to one pane, Back, open hook 5');
   await page.setViewportSize({ width: 800, height: 500 });
   await page.waitForFunction(() => document.querySelector('.settings-shell .st-coll')?.dataset.replace === 'detail', null, { timeout: 5000 });
   await button(page, 'Back to Hooks').click();
   await row('Check hook 5').click();
   await title('Check hook 5').waitFor();

   await mark('switch to a chat, widen');
   await openFromSidebar(page, SEEDED.no_project);
   await page.waitForFunction(() => !document.querySelector('.settings-shell')?.checkVisibility(), null, { timeout: 5000 });
   await page.setViewportSize({ width: 1280, height: 500 });
   await page.waitForTimeout(500);
   await mark('come back to Settings');
   await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await title('Check hook 5').waitFor();
   await page.waitForFunction(() => !document.querySelector('.settings-shell .st-coll')?.dataset.replace, null, { timeout: 5000 });
   await page.waitForTimeout(300);
   expect(await pane() === 0, `hook 5 opens at its top beside the list (${await pane()})`);
  }
 },
 {
  // Review of #1607/#1608: an open detail moves between its own pane (split)
  // and the page scroller (stacked, one pane) when the window changes size.
  // It keeps its own place each way, never the list's offset.
  name: 'layout-switch-keeps-detail-place', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stubHooks(page);
   await page.setViewportSize({ width: 1440, height: 900 });
   await gotoApp(page, status);
   await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await settingsNav(page).waitFor();
   await category(page, 'Hooks');
   await hookRow(page).waitFor();
  },
  async act({ page, mark }) {
   const title = name => page.locator('[data-settings-heading="detail"]').filter({ hasText: name }).filter({ visible: true }).first();
   const row = name => page.locator('.st-lcol [data-settings-item]').filter({ hasText: new RegExp(`^\\s*${name}\\b`) }).filter({ visible: true }).first();
   // The title's distance below the top of whatever shows the detail.
   const place = () => title('Check hook 9').evaluate(el => {
    const view = document.querySelector('.settings-viewport');
    const pane = el.closest('.st-dpane');
    const box = view.getBoundingClientRect();
    const bar = [...document.querySelectorAll('[role="banner"]')].find(b => b.checkVisibility());
    const barBottom = bar ? bar.getBoundingClientRect().bottom : box.top;
    const split = !el.closest('.settings-shell').hasAttribute('data-stacked') && !el.closest('.st-coll').dataset.replace;
    return { title: Math.round(el.getBoundingClientRect().top - Math.max(box.top, barBottom)), page: Math.round(view.scrollTop), pane: Math.round(pane.scrollTop), split };
   });
   // Split: list beside the detail. Otherwise stacked or one pane at a time.
   const sizes = async (width, height, split) => {
    await page.setViewportSize({ width, height });
    await page.waitForFunction(s => {
     const coll = document.querySelector('.settings-shell .st-coll');
     return !!coll && (!coll.closest('.settings-shell').hasAttribute('data-stacked') && !coll.dataset.replace) === s;
    }, split, { timeout: 5000 });
    await page.waitForTimeout(400);
    return place();
   };

   await mark('open hook 9 beside the list, scroll the list to its end');
   await row('Check hook 9').click();
   await title('Check hook 9').waitFor();
   await page.evaluate(() => { const v = document.querySelector('.settings-viewport'); v.scrollTo({ top: v.scrollHeight }); });
   await page.waitForTimeout(300);
   const start = await place();
   expect(start.split && start.page > 100 && start.pane === 0, `list scrolled, pane at its top (${JSON.stringify(start)})`);
   const top = start.title;

   await mark('phone size: the detail shows from its top, not the list offset');
   let at = await sizes(390, 844, false);
   expect(at.title >= 0 && at.title <= top + 40, `stacked detail at its top (${JSON.stringify(at)}, split title ${top})`);

   await mark('back to wide, then sideways phone');
   await sizes(1440, 900, true);
   at = await sizes(844, 390, false);
   expect(at.title >= 0 && at.title <= top + 40, `one-pane detail at its top (${JSON.stringify(at)})`);

   await mark('scroll the detail one pane at a time, widen: the pane keeps that place');
   await page.evaluate(() => document.querySelector('.settings-viewport').scrollBy({ top: 200 }));
   await page.waitForTimeout(200);
   const before = await place();
   at = await sizes(1440, 900, true);
   expect(Math.abs(at.title - before.title) <= 40 && at.pane > 100, `split pane keeps the detail's place (${JSON.stringify(before)} -> ${JSON.stringify(at)})`);

   await mark('narrow to a phone: the page shows the same place');
   at = await sizes(390, 844, false);
   expect(Math.abs(at.title - before.title) <= 40, `stacked keeps the detail's place (${JSON.stringify(before)} -> ${JSON.stringify(at)})`);
  }
 },
 {
  // Review of the layout switch: the list scrolled while stacked (the detail
  // pane not shown), then a detail opened and the window widened to split.
  // The list comes back where the reader left it, not at its top.
  name: 'stacked-list-place-survives-widen', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stubHooks(page);
   await page.setViewportSize({ width: 390, height: 844 });
   await gotoApp(page, status);
   await (await showSidebar(page)).getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await settingsNav(page).waitFor();
   await category(page, 'Hooks');
   await hookRow(page).waitFor();
  },
  async act({ page, mark }) {
   const row = name => page.locator('.st-lcol [data-settings-item]').filter({ hasText: new RegExp(`^\\s*${name}\\b`) }).filter({ visible: true }).first();
   const title = name => page.locator('[data-settings-heading="detail"]').filter({ hasText: name }).filter({ visible: true }).first();
   const view = () => page.evaluate(() => Math.round(document.querySelector('.settings-viewport').scrollTop));
   await mark('scroll the stacked list, open hook 15');
   await page.evaluate(() => document.querySelector('.settings-viewport').scrollTo({ top: 700 }));
   await page.waitForTimeout(300);
   const listAt = await view();
   expect(listAt > 400, `the stacked list scrolls (${listAt})`);
   await row('Check hook 15').click();
   await title('Check hook 15').waitFor();
   await page.waitForTimeout(300);

   await mark('widen to split: the list is where it was');
   await page.setViewportSize({ width: 1440, height: 500 });
   await page.waitForFunction(() => { const c = document.querySelector('.settings-shell .st-coll'); return !!c && !c.closest('.settings-shell').hasAttribute('data-stacked') && !c.dataset.replace; }, null, { timeout: 5000 });
   await page.waitForTimeout(400);
   const at = await row('Check hook 15').evaluate(el => {
    const v = document.querySelector('.settings-viewport');
    const box = v.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return { page: Math.round(v.scrollTop), row: [Math.round(r.top), Math.round(r.bottom)], view: [Math.round(box.top), Math.round(box.bottom)], pane: Math.round(document.querySelector('.settings-viewport .st-dpane').scrollTop) };
   });
   expect(at.page > 100 && at.row[0] >= at.view[0] - 1 && at.row[1] <= at.view[1] + 1, `split list keeps the reader's place, hook 15 on screen (stacked ${listAt} -> ${JSON.stringify(at)})`);
   expect(at.pane === 0, `the detail beside it is at its top (${at.pane})`);

   // Split -> stacked -> split with no scrolling: both come back. The browser
   // clamping the page in between is not the reader's place.
   await mark('scroll the detail pane, turn to the phone and back');
   await page.setViewportSize({ width: 1440, height: 300 });
   await page.waitForTimeout(400);
   const placed = await page.evaluate(async () => {
    const pane = document.querySelector('.settings-viewport .st-dpane');
    pane.scrollTo({ top: Math.min(90, pane.scrollHeight - pane.clientHeight) });
    await new Promise(r => setTimeout(r, 300));
    return { page: Math.round(document.querySelector('.settings-viewport').scrollTop), pane: Math.round(pane.scrollTop) };
   });
   expect(placed.pane > 20, `the split detail pane scrolls (${JSON.stringify(placed)})`);
   await page.setViewportSize({ width: 390, height: 844 });
   await page.waitForFunction(() => document.querySelector('.settings-shell .st-coll')?.closest('.settings-shell').hasAttribute('data-stacked'), null, { timeout: 5000 });
   await page.waitForTimeout(400);
   await page.setViewportSize({ width: 1440, height: 300 });
   await page.waitForFunction(() => !document.querySelector('.settings-shell .st-coll')?.closest('.settings-shell').hasAttribute('data-stacked'), null, { timeout: 5000 });
   await page.waitForTimeout(400);
   const back = await page.evaluate(() => ({ page: Math.round(document.querySelector('.settings-viewport').scrollTop), pane: Math.round(document.querySelector('.settings-viewport .st-dpane').scrollTop) }));
   expect(Math.abs(back.page - placed.page) <= 1 && Math.abs(back.pane - placed.pane) <= 1, `both panes return after the round trip (${JSON.stringify(placed)} -> ${JSON.stringify(back)})`);

   // A scroll made right after a switch, before the next frame, is the
   // reader's place too (review: a timed guard dropped it).
   await mark('turn and back, scrolling the pane the moment it is split again');
   const target = Math.max(1, Math.round(placed.pane / 2));
   await page.evaluate(t => {
    const shell = document.querySelector('.settings-shell .st-coll').closest('.settings-shell');
    const mo = new MutationObserver(() => {
     if (shell.hasAttribute('data-stacked')) return;
     mo.disconnect();
     setTimeout(() => document.querySelector('.settings-viewport .st-dpane').scrollTo({ top: t }), 0);
    });
    mo.observe(shell, { attributes: true, attributeFilter: ['data-stacked'] });
   }, target);
   await page.setViewportSize({ width: 390, height: 844 });
   await page.waitForFunction(() => document.querySelector('.settings-shell .st-coll')?.closest('.settings-shell').hasAttribute('data-stacked'), null, { timeout: 5000 });
   await page.waitForTimeout(400);
   await page.setViewportSize({ width: 1440, height: 300 });
   await page.waitForFunction(() => !document.querySelector('.settings-shell .st-coll')?.closest('.settings-shell').hasAttribute('data-stacked'), null, { timeout: 5000 });
   await page.waitForTimeout(400);
   const moved = await page.evaluate(() => Math.round(document.querySelector('.settings-viewport .st-dpane').scrollTop));
   expect(Math.abs(moved - target) <= 1, `the pane is where it was scrolled to (${moved}, wanted ${target})`);
   await page.setViewportSize({ width: 390, height: 844 });
   await page.waitForFunction(() => document.querySelector('.settings-shell .st-coll')?.closest('.settings-shell').hasAttribute('data-stacked'), null, { timeout: 5000 });
   await page.waitForTimeout(400);
   await page.setViewportSize({ width: 1440, height: 300 });
   await page.waitForFunction(() => !document.querySelector('.settings-shell .st-coll')?.closest('.settings-shell').hasAttribute('data-stacked'), null, { timeout: 5000 });
   await page.waitForTimeout(400);
   const kept = await page.evaluate(() => Math.round(document.querySelector('.settings-viewport .st-dpane').scrollTop));
   expect(Math.abs(kept - target) <= 1, `that scroll is kept across the next turn (${kept}, wanted ${target}, not ${placed.pane})`);
  }
 },
 {
  // Review: a switch made while Settings is hidden is applied once it shows.
  name: 'one-pane-hidden-widen-keeps-place', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stubHooks(page);
   await page.setViewportSize({ width: 800, height: 500 });
   await gotoApp(page, status);
   await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await settingsNav(page).waitFor();
   await category(page, 'Hooks');
   await hookRow(page).waitFor();
  },
  async act({ page, mark }) {
   const row = name => page.locator('.st-lcol [data-settings-item]').filter({ hasText: new RegExp(`^\\s*${name}\\b`) }).filter({ visible: true }).first();
   const title = name => page.locator('[data-settings-heading="detail"]').filter({ hasText: name }).filter({ visible: true }).first();
   await mark('open hook 12 one pane at a time and scroll it');
   await row('Check hook 12').click();
   await title('Check hook 12').waitFor();
   await page.waitForFunction(() => document.querySelector('.settings-shell .st-coll')?.dataset.replace === 'detail', null, { timeout: 5000 });
   await page.waitForTimeout(300);
   const offset = await page.evaluate(async () => {
    const v = document.querySelector('.settings-viewport');
    v.scrollTo({ top: v.scrollTop + 300 });
    await new Promise(r => setTimeout(r, 300));
    const p = v.querySelector('.st-dpane');
    return Math.round(v.getBoundingClientRect().top - p.getBoundingClientRect().top);
   });
   expect(offset > 100, `the detail scrolls in the page (${offset})`);

   await mark('switch to a chat, widen');
   await openFromSidebar(page, SEEDED.no_project);
   await page.waitForFunction(() => !document.querySelector('.settings-shell')?.checkVisibility(), null, { timeout: 5000 });
   await page.setViewportSize({ width: 1280, height: 500 });
   await page.waitForTimeout(500);
   await mark('come back to Settings, same item');
   await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
   await title('Check hook 12').waitFor();
   await page.waitForFunction(() => !document.querySelector('.settings-shell .st-coll')?.dataset.replace, null, { timeout: 5000 });
   await page.waitForTimeout(300);
   const at = await page.evaluate(() => { const p = document.querySelector('.settings-viewport .st-dpane'); return { top: Math.round(p.scrollTop), max: p.scrollHeight - p.clientHeight }; });
   const want = Math.min(offset, at.max);
   expect(Math.abs(at.top - want) <= 1, `hook 12 beside the list keeps its place (${JSON.stringify(at)}, wanted ${want})`);
  }
 },
 {
  name: 'same-logger-after-stacking-two-details', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stub(page);
   await openSettings(page, status, 1280);
  },
  async act({ page, mark }) {
   await category(page, 'Integrations');
   const beforeLogs = await entryKey(page);
   await category(page, 'Logs');
   const list = await entryKey(page);
   const row = text => page.locator('.st-lcol [data-settings-item]').filter({ hasText: text }).first();
   const detail = text => page.locator('.st-dpane').getByText(text, { exact: true }).filter({ visible: true }).first();
   await mark('open two log entries side by side');
   await row('Beta is slow').click(); await detail('Beta is slow').waitFor();
   const first = await entryKey(page);
   await row('Alpha ready').click(); await detail('Alpha ready').waitFor();
   const second = await entryKey(page);

   await mark('shrink to the phone stack and show the same logger');
   await page.setViewportSize({ width: 390, height: 844 });
   await page.locator('.settings-shell[data-stacked][data-view="detail"]').waitFor();
   await button(page, 'Same logger').click();
   await page.waitForFunction(key => navigation.currentEntry.key !== key, second);
   expect(await entryKey(page) === list, `Same logger returns to the original list, not another detail (${await visibleLogRows(page)} rows visible)`);
   await row('Alpha ready').waitFor();
   expect(await listShown(page) && await visibleLogRows(page) === 2, 'only the filtered list is shown');
   expect(await focusInList(page), 'focus returns to the visible list');

   await mark('Back leaves Logs; Forward retraces the list and both details');
   await page.goBack();
   await page.getByRole('heading', { name: 'Integrations', exact: true, level: 1 }).filter({ visible: true }).first().waitFor();
   expect(await entryKey(page) === beforeLogs, 'one Back reaches the category before Logs');
   await page.goForward(); await row('Alpha ready').waitFor();
   expect(await entryKey(page) === list, 'Forward restores the original list');
   await page.goForward(); await detail('Beta is slow').waitFor();
   expect(await entryKey(page) === first, 'first detail survives list return');
   await page.goForward(); await detail('Alpha ready').waitFor();
   expect(await entryKey(page) === second, 'second detail survives list return');
  }
 },
 ...[390, 800].map(width => ({
  name: `resize-focused-logs-${width}`, viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   // The search field appears above six lines.
   await stub(page, { lines: [...LOGS, ...['41', '40', '39'].map(min => line(min, 'INFO', 'app.journey.other', `Line ${min}`))] });
   await openSettings(page, status, 1280);
  },
  async act({ page, mark }) {
   await category(page, 'Logs');
   const row = page.locator('.st-lcol [data-settings-item]').filter({ hasText: 'Alpha started' }).first();
   await row.click();
   const selected = await row.getAttribute('data-settings-item');
   await category(page, 'Integrations');
   await settingsNav(page).getByRole('button', { name: /^Logs\b/ }).click();
   const action = button(page, 'Same logger');
   await action.waitFor();
   const list = await entryKey(page);
   const search = page.getByRole('searchbox', { name: 'Search logs' });
   const resize = async w => {
    await page.setViewportSize({ width: w, height: 800 });
    await action.waitFor({ state: w === 1280 ? 'visible' : 'hidden' });
    expect(await entryKey(page) === list, 'resizing did not navigate');
   };
   await mark('keep typing at the caret when the focused search stays visible');
   await search.fill('pha'); await search.press('Home');
   await resize(width);
   await page.keyboard.type('l');
   expect(await search.inputValue() === 'lpha', 'narrowing preserves focus and the insertion point');
   await search.press('Home'); await resize(1280); await page.keyboard.type('A');
   expect(await search.inputValue() === 'Alpha', 'widening preserves focus and the insertion point');
   await mark('recover focus when the focused detail control disappears');
   await action.focus(); await resize(width);
   expect(await focusedItem(page) === selected, 'hidden detail yields focus to its visible row');
  }
 })),
 ...[390, 800].map(width => ({
  name: `same-logger-retained-list-${width}`, viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) { await stub(page); await openSettings(page, status, 1280); },
  async act({ page, mark }) {
   const row = name => page.locator('.st-lcol [data-settings-item]').filter({ hasText: name }).first();
   const detail = name => page.locator('.st-dpane').getByText(name, { exact: true }).filter({ visible: true }).first();
   await mark('revisit Logs with a selected row beside its list');
   await category(page, 'Logs');
   await row('Alpha started').click(); await detail('Alpha started').waitFor();
   await category(page, 'Integrations');
   const before = await entryKey(page);
   // The Logs nav label now includes the error count.
   await settingsNav(page).getByRole('button', { name: /^Logs\b/ }).click();
   await detail('Alpha started').waitFor();
   const list = await entryKey(page);
   await row('Alpha ready').click(); await detail('Alpha ready').waitFor();
   const opened = await entryKey(page);
   await mark('return to the filtered list where the detail replaces it');
   await page.setViewportSize({ width, height: 844 });
   await row('Alpha ready').waitFor({ state: 'hidden' });
   await button(page, 'Same logger').click(); await row('Alpha ready').waitFor();
   expect(await entryKey(page) === list, 'returned to the existing list entry despite its retained selection');
   expect(await visibleLogRows(page) === 2 && await focusInList(page), 'two filtered rows and focus in the list');
   expect(await focusedItem(page) === await row('Alpha ready').getAttribute('data-settings-item'), 'Same logger returns focus to its row');
   expect(!await detail('Alpha started').count(), 'retained detail does not displace the narrow list');
   await mark('Back leaves Logs; Forward retraces the list and detail');
   await page.goBack();
   await page.getByRole('heading', { name: 'Integrations', exact: true, level: 1 }).filter({ visible: true }).first().waitFor();
   expect(await entryKey(page) === before, 'one Back leaves Logs');
   await page.goForward(); await row('Alpha ready').waitFor();
   expect(await entryKey(page) === list, 'Forward lands on the original list');
   await page.setViewportSize({ width: 1280, height: 800 }); await detail('Alpha started').waitFor();
   expect(await entryKey(page) === list && await visibleLogRows(page) === 2, 'widening reveals the retained selection without navigating');
   await page.goForward(); await detail('Alpha ready').waitFor();
   expect(await entryKey(page) === opened, 'Forward restores the explicit detail entry');
  }
 })),
 {
  name: 'wide-split-unchanged', viewports: ['desktop'], throttles: ['fast'],
  async setup({ page, status }) {
   await stub(page, { users: [user] });
   await openSettings(page, status, 1280);
  },
  async act({ page, mark }) {
   await mark('Integrations: the open item sits beside the list');
   await category(page, 'Integrations');
   await page.locator('.st-lcol [data-settings-item="u1"]').filter({ visible: true }).first().click();
   await page.getByRole('heading', { name: 'Build bot', exact: true, level: 2 }).filter({ visible: true }).first().waitFor();
   expect(await replaceState(page) === null && await listShown(page), 'list and detail side by side at 1280');
   expect(!(await page.locator('.st-dback').filter({ visible: true }).count()), 'no page Back in the split');

   await mark('Logs: Same logger keeps the entry open beside the filtered list');
   const { listEntry, detailEntry } = await sameLogger(page);
   expect(await entryKey(page) === detailEntry, 'Same logger keeps the same split detail entry');
   expect(await listShown(page) && await visibleLogRows(page) === 2, 'filtered list beside the entry');
   expect(await page.getByRole('button', { name: 'Same logger', exact: true }).filter({ visible: true }).count() === 1, 'the entry is still open');

   await mark('select another log in split view, then narrow the window');
   await page.locator('.st-lcol [data-settings-item]').filter({ hasText: 'Alpha ready' }).first().click();
   await page.setViewportSize({ width: 800, height: 800 });
   await button(page, 'Same logger').click();
   await page.locator('.st-lcol [data-settings-item]').filter({ visible: true }).first().waitFor();
   expect(await entryKey(page) === listEntry && await visibleLogRows(page) === 2, 'both details lead back to the original filtered list');
  }
 }
] });
