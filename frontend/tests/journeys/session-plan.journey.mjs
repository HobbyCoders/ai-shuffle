// Real native/delegated Plan handlers write canonical state. No display-field seeding.
import { fakeAiTag, runJourney } from './runner.mjs';
import { PROJECT, expect, openNewChat, pick, pickAccess, settleServiceWorker, typeAndSend, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const plans = page => page.getByRole('region', { name: 'Session plan' });
const revision = (page, n) => page.locator(`[aria-label="Session plan"][data-plan-revision="${n}"]`);
const legacyNote = 'Legacy scratch remains a thought, not a completed step';

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
 const measure = () => button.evaluate(button => {
  const scroller = button.closest('.msg-scroll');
  const box = button.getBoundingClientRect();
  return { top: box.top, bottom: box.bottom, focused: document.activeElement === button,
   scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight,
   viewportTop: scroller.getBoundingClientRect().top, viewportBottom: scroller.getBoundingClientRect().bottom };
 });
 const before = await measure();
 await activate(button);
 // Let the real column ResizeObserver and reserve handoff finish, including at 4x CPU.
 await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(done)))));
 const after = await measure();
 log(`${label}: ${JSON.stringify({ before, after })}`);
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
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 3000);
  const out = [];
  try {
   const response = await fetch(`/api/v1/sessions/${id}/events?from_seq=0`, { signal: abort.signal });
   const reader = response.body.getReader();
   let text = '';
   while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    text += new TextDecoder().decode(value);
    const lines = text.split('\n'); text = lines.pop();
    for (const line of lines) {
     if (!line.startsWith('data:')) continue;
     const data = JSON.parse(line.slice(5));
     if (data.type === 'caught_up') return out;
     if (['tool.use', 'tool.result', 'plan.updated', 'turn.aborted'].includes(data.type)) out.push(data);
    }
   }
  } catch (error) { out.push({ diagnostic: String(error) }); }
  finally { clearTimeout(timer); abort.abort(); }
  return out;
 }, id);
}

function planCase(delegated) {
 return {
  name: delegated ? 'delegated-plan' : 'native-plan',
  setup: ctx => setupChat(ctx, delegated),
  async act({ page, variant, mark, log }) {
   await mark('produce plan');
   const id = await produce(page, variant, delegated ? 'plan-delegated' : 'plan');
   const state = await readPlan(page, id);
   if (state.revision !== 1) log(`unexpected producer state: ${JSON.stringify(await diagnosticEvents(page, id)).slice(0, 6000)}`);
   expect(state.revision === 1, `real handler persisted revision one (got ${state.revision})`);
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
  // End is real keyboard input to the transcript, not a synthetic scroll event.
  await page.getByRole('log', { name: 'Conversation messages' }).focus();
  await page.keyboard.press('Control+End');
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
   log(`lifecycle events: ${JSON.stringify(await diagnosticEvents(page, id)).slice(-14000)}`);
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
