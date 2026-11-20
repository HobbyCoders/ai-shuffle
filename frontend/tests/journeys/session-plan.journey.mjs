// Real native/delegated Plan handlers write canonical state. No display-field seeding.
import { fakeAiTag, runJourney } from './runner.mjs';
import { PROJECT, api, expect, openNewChat, openPopup, pick, pickAccess, settleServiceWorker, typeAndSend, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const plans = page => page.getByRole('region', { name: 'Session plan' });
const revision = (page, n) => page.locator(`[aria-label="Session plan"][data-plan-revision="${n}"]`);
const legacyNote = 'Legacy scratch remains a thought, not a completed step';
const planModel = (status, delegated) => delegated ? 'sonnet' : `prov:${status.seed.provider_id}:${status.seed.model}`;

async function setupChat(ctx, delegated = false) {
 await openNewChat(ctx, { project: PROJECT });
 await settleServiceWorker(ctx.page, ctx.status);
 const hero = await openNewChat(ctx, { project: PROJECT });
 if (delegated) {
  const menu = ctx.page.getByRole('menu', { name: 'Agent selection' });
  await pick(hero.getByRole('button', { name: /^Agent:/ }), menu,
   menu.getByRole('button', { name: 'Dev (Claude Code)', exact: true }),
   hero.getByRole('button', { name: 'Agent: Dev (Claude Code)' }));
 }
 await pickAccess(ctx.page, 'Bypass');
 // Agent selection doesn't change the model binding. A previous journey can
 // leave a saved composer model on this worker, even in a fresh browser context.
 const { page, status } = ctx;
 const model = planModel(status, delegated);
 const picker = page.getByRole('dialog', { name: 'Choose model' });
 await openPopup(page.getByRole('button', { name: /^Model: / }).filter({ visible: true }), picker);
 await picker.getByRole('option').and(picker.locator(`[data-value="${model}"]`)).click();
 if (await picker.isVisible()) await page.keyboard.press('Escape');
 await picker.waitFor({ state: 'hidden' });
}

async function produce(page, variant, script) {
 const feed = page.waitForRequest(r => /\/sessions\/[^/]+\/events(?:\?|$)/.test(r.url()));
 await typeAndSend(page, `Exercise structured planning ${fakeAiTag(variant, script)}`);
 const id = (await feed).url().match(/\/sessions\/([^/]+)\/events/)[1];
 await waitTurnSettled(page, { messagesAtLeast: 4 });
 return id;
}

// Geometry, not just aria-expanded: following the tail used to move an older
// focused Plan header offscreen by exactly the height its details added.
async function assertDisclosurePosition(page, plan, activate, log, label) {
 const button = plan.getByRole('button', { name: /(?:Expand|Collapse) plan/ });
 await button.focus();
 const preLocator = await button.evaluate(button => {
  const measure = () => {
   const scroller = button.closest('.msg-scroll');
   const box = button.getBoundingClientRect();
   return { top: box.top, bottom: box.bottom, focused: document.activeElement === button,
    scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight,
    viewportTop: scroller.getBoundingClientRect().top, viewportBottom: scroller.getBoundingClientRect().bottom, windowY: scrollY };
  };
  // Locator click may reveal a header near the scroll-padding edge BEFORE
  // dispatch. Observe its actual click before Svelte's target handler changes
  // layout; retain the pre-locator coordinates to distinguish that reveal.
  button.__planDisclosureProbe = { measure, before: null };
  button.addEventListener('click', () => { button.__planDisclosureProbe.before = measure(); }, { capture: true, once: true });
  return measure();
 });
 await activate(button);
 // Let the real column ResizeObserver and reserve handoff finish, including at 4x CPU.
 await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(done)))));
 const { before, after } = await button.evaluate(button => {
  const { before, measure } = button.__planDisclosureProbe;
  delete button.__planDisclosureProbe;
  return { before, after: measure() };
 });
 expect(!!before, `${label}: activation dispatched a real disclosure click`);
 log(`${label}: ${JSON.stringify({ preLocator, before, after })}`);
 const clamp = Math.max(0, before.scrollTop - (after.scrollHeight - after.clientHeight));
 expect(Math.abs(after.top - before.top - clamp) <= 2, `${label}: focused header stays put (except physical bottom clamp)`);
 expect(after.focused && after.bottom > after.viewportTop && after.top < after.viewportBottom, `${label}: focus stays visible`);
}

async function readPlan(page, id) {
 const response = await page.request.get(new URL(`/api/v1/sessions/${id}/plan`, page.url()).href);
 expect(response.ok(), 'canonical projection is readable');
 return (await response.json()).plan;
}

async function diagnosticEvents(page, id) {
 return page.evaluate(async id => {
  const feed = new EventSource(`/api/v1/sessions/${id}/events?from_seq=0`);
  const out = [];
  let timer;
  try {
   return await new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error('Timed out waiting for caught_up in session event replay')), 3000);
    feed.addEventListener('error', () => reject(new Error('Session event replay ended or failed before caught_up')));
    // The replay boundary is a named SSE event, not a type in its JSON data.
    feed.addEventListener('caught_up', () => resolve(out));
    for (const type of ['tool.use', 'tool.result', 'plan.updated', 'turn.aborted']) {
     feed.addEventListener(type, event => {
      try { out.push(JSON.parse(event.data)); }
      catch (error) { reject(error); }
     });
    }
   });
  } finally {
   clearTimeout(timer);
   feed.close();
  }
 }, id);
}

function planCase(delegated) {
 return {
  name: delegated ? 'delegated-plan' : 'native-plan',
  setup: ctx => setupChat(ctx, delegated),
  async act({ page, status, variant, mark, log }) {
   await mark('produce plan');
   const id = await produce(page, variant, delegated ? 'plan-delegated' : 'plan');
   const state = await readPlan(page, id);
   const { model, provider_id: provider } = await api(page, 'GET', `/sessions/${id}`);
   const events = await diagnosticEvents(page, id);
   log(`session ${id}: model=${model}, provider=${provider}, plan revision=${state.revision}`);
   if (state.revision !== 1) log(`unexpected producer state: ${JSON.stringify(events).slice(0, 6000)}`);
   expect(state.revision === 1, `real handler persisted revision one (got ${state.revision})`);
   expect(model === planModel(status, delegated) && provider === (delegated ? null : status.seed.provider_id),
    'the session saved the model and provider chosen for this leg');
   const calls = events.filter(e => e.type === 'tool.use' && (delegated
    ? e.name === 'mcp__fluid_bundled__Plan'
    : e.name === 'run_python' && e.input?.code?.includes('from tools.Plan import Plan')));
   expect(calls.length === 2, `the ${delegated ? 'delegated MCP' : 'native code-mode'} surface ran both Plan calls`);
   if (delegated) expect(calls[0].input.action === 'update' && calls[1].input.action === 'get', 'delegated Plan updates then reads');
   for (const call of calls) {
    expect(events.some(e => e.type === 'tool.result' && e.tool_use_id === call.tool_use_id && e.is_error === false),
     `${call.name} returned a successful result for ${call.tool_use_id}`);
   }
   log(`successful Plan calls: ${calls.map(call => call.name).join(', ')}`);
   const plan = plans(page).last();
   await plan.waitFor({ timeout: 8000 });
   await mark('expand plan');
   if (variant.viewport === 'desktop') {
    await plan.getByRole('button', { name: /Expand plan/ }).focus();
    await page.keyboard.press('Enter');
   } else await plan.getByRole('button', { name: /Expand plan/ }).click();
   await plan.getByText('Prove session-owned planning', { exact: false }).waitFor();
   await plan.getByText('No real provider credential in private dev copy', { exact: true }).waitFor();
   expect((await plan.innerText()).includes('Model-reported · not independently verified'), 'status provenance stays explicit');
   expect(await plan.locator('[data-plan-task]').count() === 3, 'all produced tasks appear once');
   await mark('collapse plan');
   await plan.getByRole('button', { name: /Collapse plan/ }).click();
   expect(await plan.getByRole('list').count() === 0, 'collapsed plan hides task details');
   await mark('reload durable snapshot');
   await page.reload({ waitUntil: 'domcontentloaded' });
   await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
   await mark('page reloaded');
   await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 4 });
   await plan.waitFor({ timeout: 15000 });
   expect(await plans(page).count() === 1, 'cold replay has one immutable snapshot');
   await plan.getByRole('button', { name: /Expand plan/ }).click();
   await plan.getByText('Snapshot · plan 1 · revision 1', { exact: true }).waitFor();
   await plan.getByText('No real provider credential in private dev copy', { exact: true }).waitFor();
  }
 };
}

const lifecycle = {
 name: 'native-lifecycle',
 setup: ctx => setupChat(ctx),
 async act({ page, variant, mark, log }) {
  await mark('produce legacy scratch through Note');
  const id = await produce(page, variant, 'plan-note-setup');
  try {
  const noteLogs = page.getByRole('button', { name: /^Show work details/ });
  if (await noteLogs.count()) await noteLogs.last().click();
  await page.getByText(legacyNote, { exact: true }).last().waitFor();
  const noteBefore = await page.locator('.notes').last().innerText();
  await mark('produce initial Plan');
  await typeAndSend(page, fakeAiTag(variant, 'plan'));
  await revision(page, 1).waitFor();
  await waitTurnSettled(page, { messagesAtLeast: 6 });
  await mark('report blocker cancellation and evidence');
  await typeAndSend(page, fakeAiTag(variant, 'plan-update'));
  await revision(page, 2).waitFor();
  await waitTurnSettled(page, { messagesAtLeast: 8 });
  const second = revision(page, 2);
  await second.getByRole('button', { name: /Expand plan/ }).click();
  await second.getByText('Controlled fixture is waiting for owner review; no worker or external job was started.', { exact: true }).waitFor();
  await second.getByText('Controlled fixture retains the initial reported completion; not independent verification.', { exact: true }).waitFor();
  expect(await second.getByText('1 cancelled', { exact: true }).count() === 1, 'cancelled is explicit, not success');
  expect(await second.getByText('session-plan:revision:1', { exact: true }).count() === 2, 'opaque evidence references render as reported text');
  expect(await second.locator('a').count() === 0, 'opaque references never become fabricated links');
  await revision(page, 1).getByRole('button', { name: /Expand plan/ }).click();
  await revision(page, 1).getByText('No real provider credential in private dev copy', { exact: true }).waitFor();
  await mark('complete old goal and explicitly roll over');
  await typeAndSend(page, fakeAiTag(variant, 'plan-complete'));
  await revision(page, 4).waitFor({ timeout: 30000 });
  await waitTurnSettled(page, { messagesAtLeast: 10 });
  expect(await plans(page).count() === 4, 'each persisted revision has its own anchored snapshot');
  expect((await readPlan(page, id)).generation === 2, 'latest canonical API state is the new goal');
  await revision(page, 3).getByRole('button', { name: /Expand plan/ }).click();
  await revision(page, 3).getByText('1 cancelled', { exact: true }).waitFor();
  expect(await revision(page, 3).getByText('All steps completed', { exact: true }).count() === 0, 'terminal with cancellation is not all-success');
  await revision(page, 4).getByRole('button', { name: /Expand plan/ }).click();
  await revision(page, 4).getByText('Snapshot · plan 2 · revision 4', { exact: true }).waitFor();
  await revision(page, 4).getByText('Second controlled session goal', { exact: false }).waitFor();
  await mark('historical Plan disclosure preserves the reader');
  const historical = revision(page, 3);
  await historical.getByRole('button', { name: /Collapse plan/ }).click();
  const jump = page.getByRole('button', { name: 'Jump to latest', exact: true });
  if (await jump.isVisible()) await jump.click();
  // Native End scrolls animate (and the log is not focusable). Measuring in
  // that animation mistakes its remaining travel for disclosure-induced drift.
  // Use real input inside the scroller and prove it reached the bottom first.
  await page.getByRole('log', { name: 'Conversation messages' }).hover();
  await page.mouse.wheel(0, 10000);
  await page.waitForFunction(() => {
   const s = document.querySelector('.msg-scroll');
   return s && Math.abs(s.scrollHeight - s.clientHeight - s.scrollTop) <= 1;
  });
  await assertDisclosurePosition(page, historical, button => button.click(), log, 'historical pointer expand');
  await assertDisclosurePosition(page, historical, () => page.keyboard.press('Enter'), log, 'historical keyboard collapse');
  await assertDisclosurePosition(page, revision(page, 4), () => page.keyboard.press('Enter'), log, 'current keyboard collapse');
  await mark('read original Note and current Plan without conversion');
  await typeAndSend(page, fakeAiTag(variant, 'plan-note'));
  await waitTurnSettled(page, { messagesAtLeast: 12 });
  if (await noteLogs.count()) await noteLogs.last().click();
  const note = page.locator('.notes').last();
  await note.getByText(legacyNote, { exact: true }).waitFor();
  expect(await note.innerText() === noteBefore, 'legacy scratch rendering is unchanged after planning');
  expect(await note.locator('.wlbar, .is-done').count() === 0, 'notes never acquire todo statuses');
  expect((await readPlan(page, id)).revision === 4, 'read-only Note/get never mutates Plan');
  await mark('reload latest and historical snapshots');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  await mark('page reloaded');
  await waitChat(page, { surface: 'active', loading: 'false', messagesAtLeast: 12 });
  await revision(page, 4).waitFor();
  expect(await plans(page).count() === 4, 'cold load preserves all four snapshots once');
  await revision(page, 2).getByRole('button', { name: /Expand plan/ }).click();
  await revision(page, 2).getByText('Controlled fixture is waiting for owner review; no worker or external job was started.', { exact: true }).waitFor();
  } catch (error) {
   log(`lifecycle session: ${id}`);
   log(`lifecycle API state: ${JSON.stringify(await readPlan(page, id))}`);
   const events = await diagnosticEvents(page, id).catch(error => ({ diagnostic: String(error) }));
   log(`lifecycle events: ${JSON.stringify(events).slice(-14000)}`);
   log(`lifecycle visible state: ${(await page.locator('body').innerText()).slice(-4000)}`);
   throw error;
  }
 }
};

await runJourney({
 name: 'session-plan',
 cases: [planCase(false), planCase(true), lifecycle],
 allow: [...DEV_INSTANCE_NOISE]
});
