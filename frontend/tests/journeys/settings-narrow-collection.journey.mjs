// Settings collections in a window too narrow for list and detail side by side
// (#1406): the open item replaces the list and nothing spills past the pane,
// Back returns to the list with focus on the row, an item that no longer exists
// can still be left, and Logs "Same logger" shows its filtered list. A guard at
// 1280 keeps the split unchanged. API users and logs are route stubs, so the
// worker's real data never decides the result.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
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

async function stub(page, { users = [] } = {}) {
 await page.route(/\/api\/v1\/api-users(\?.*)?$/, route => route.fulfill({ json: users }));
 await page.route(/\/api\/v1\/logs\?/, route => route.fulfill({ json: { lines: LOGS, total: LOGS.length, returned: LOGS.length, levels: ['INFO', 'WARNING', 'ERROR'] } }));
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

async function sameLogger(page) {
 await category(page, 'Logs');
 await page.locator('.st-lcol [data-settings-item]').filter({ visible: true }).first().waitFor();
 expect(await visibleLogRows(page) === 4, 'Logs lists the four stubbed lines');
 await page.locator('.st-lcol [data-settings-item]').filter({ hasText: 'Alpha started' }).first().click();
 await button(page, 'Same logger').waitFor();
 await button(page, 'Same logger').click();
 await page.waitForFunction(() => [...document.querySelectorAll('.st-lcol [data-settings-item]')].filter(el => el.checkVisibility()).length === 2, null, { timeout: 5000 }).catch(() => {});
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
   await sameLogger(page);
   expect(await listShown(page), 'the filtered list is on screen');
   expect(await visibleLogRows(page) === 2, `two alpha lines listed (${await visibleLogRows(page)})`);
   expect(await replaceState(page) !== 'detail', 'no entry hides the list');
   expect(await focusInList(page), 'focus is in the list');
  }
 },
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
   await sameLogger(page);
   expect(await listShown(page) && await visibleLogRows(page) === 2, 'filtered list beside the entry');
   expect(await page.getByRole('button', { name: 'Same logger', exact: true }).filter({ visible: true }).count() === 1, 'the entry is still open');
  }
 }
] });
