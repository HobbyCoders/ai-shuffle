// Journey: open a mirrored chat (one another worker owns) from the
// new-session screen.
//
// A mirrored chat is checked against its owner before it is shown, and the
// first catch-up reveals its existing live transcript. ChatView keeps the centred
// hero while a chat opened from it loads, so the check must run whatever the
// layout. Bug this guards (review of PR #807): the check lived only in the
// message column (MirrorSyncGate), so it started after the history arrived:
// the transcript showed, then a full-pane "Checking sync…" replaced it; and a
// mirror with no messages never left the hero, so it was never checked. Fix:
// ChatView runs the check (createMirrorSync) and the layout waits for it like
// a load (chatLayoutSurface).
//
// One dev instance has no second worker, so the page sees a seeded chat as
// mirrored: its session rows say `is_remote` with a peer's name, and the
// owner's sync status is scripted (two "syncing" answers, then caught up).
// Everything else (history, cursor resume, layout) is the real app.
//
// Cases (through the sidebar):
//   long-from-new      a mirrored transcript from the new-session screen: the
//                      hero says it is syncing, the header names the chat as
//                      the hero does (#812), then the transcript opens in one
//                      composer move
//   untitled-from-new  a mirror with no title yet: header and hero both say
//                      "Loading…" during the sync, never "New Chat" (#812)
//   long-from-chat     from another chat: no transcript remount (#813)
//   cleared-from-new   an empty mirror: checked too, then "Conversation cleared"
import { runJourney } from './runner.mjs';
import { SEEDED, expect, gotoApp, openFromSidebar, seededChatId, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectComposerPositions, expectNeverDecreases, frames, markTime } from './lib/timeline.mjs';

const PEER = 'Peer worker';
const OPEN = 'open the mirrored chat from the sidebar';
/** "Syncing" answers before the owner reports the opening checkpoint caught up. */
const SYNCING_POLLS = 2;

/**
 * Make the page see seeded chat `key` as mirrored from PEER, and script its
 * sync status. In the page, not page.route: the built app's service worker
 * makes the requests page.route would see (see withoutSavedAgentPick). Call
 * before the page loads; `syncPolls(page)` reads how often it was polled.
 */
async function simulateMirroredChat(page, status, key, { untitled = false } = {}) {
	await page.addInitScript(
		({ id, peer, syncingPolls, untitled }) => {
			const original = window.fetch.bind(window);
			window.__mirrorSyncPolls = 0;
			window.__mirrorReaders = [];
			const pathOf = (input) => {
				try {
					return new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
				} catch {
					return '';
				}
			};
			const markRemote = (value) => {
				if (Array.isArray(value)) return value.map(markRemote);
				if (!value || typeof value !== 'object') return value;
				const out = {};
				for (const [k, v] of Object.entries(value)) out[k] = markRemote(v);
				if (out.id === id) Object.assign(out, { is_remote: true, device_name: peer }, untitled ? { title: null } : {});
				return out;
			};
			const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
			window.fetch = async (input, init) => {
				const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
				const path = pathOf(input);
				if (path === `/api/v1/sessions/${id}/events`) {
					window.__mirrorReaders.push(Number(new URL(typeof input === 'string' ? input : input.url, location.href).searchParams.get('from_seq') || 0));
				}
				if (path === `/api/v1/sessions/${id}/sync-status`) {
					window.__mirrorSyncPolls += 1;
					const target = { seq: 1, epoch: 'simulated', tail_id: 'simulated' };
					return json(
						window.__mirrorSyncPolls <= syncingPolls
							? { state: 'syncing', scope: 'session', initial_target: target, initial_caught_up: false, initial_pending_updates: 3, pending_updates: 3 }
							: { state: 'current', scope: 'session', initial_caught_up: true, pending_updates: 0 }
					);
				}
				const response = await original(input, init);
				// Session rows and metadata (JSON only; never the event stream).
				if (method !== 'GET' || !response.ok || !path.startsWith('/api/v1/sessions') || /\/(events|stream)$/.test(path)) return response;
				if (!(response.headers.get('content-type') || '').includes('application/json')) return response;
				const body = await response.clone().json().catch(() => undefined);
				if (body === undefined) return response;
				return new Response(JSON.stringify(markRemote(body)), { status: response.status, headers: response.headers });
			};
		},
		{ id: seededChatId(status, key), peer: PEER, syncingPolls: SYNCING_POLLS, untitled }
	);
}

const syncPolls = (page) => page.evaluate(() => window.__mirrorSyncPolls ?? 0);

const fromNew = (key, opts) => async (ctx) => {
	const { page, status } = ctx;
	await simulateMirroredChat(page, status, key, opts);
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' });
	await page.waitForTimeout(1000);
};

/** The hero, not a pane in the column, says the chat is syncing with its owner. */
async function expectSyncingHero(page) {
	await page.locator('.cleared-hero').getByText(`Syncing from ${PEER}…`).waitFor({ timeout: 30_000 });
	await waitChat(page, { composer: 'centre', session: /^s:/ });
}

/** The header names the syncing chat as the hero does, never "New Chat" (#812). */
async function expectHeaderMatchesHero(page, expected) {
	const hero = (await page.locator('.cleared-hero .cleared-title').first().textContent())?.trim();
	const header = (await page.locator('.title-trigger:visible').first().textContent())?.trim();
	expect(hero === expected, `the syncing hero is titled ${JSON.stringify(expected)}, got ${JSON.stringify(hero)}`);
	expect(header === expected, `the header is titled ${JSON.stringify(expected)} while the hero syncs, got ${JSON.stringify(header)}`);
}

/** No frame ever showed the column's full-pane sync check, or rows under the syncing hero. */
function expectSyncedInTheHero(recording) {
	const from = markTime(recording, OPEN);
	const pane = frames(recording).find(({ t, state }) => t >= from && Number(state.syncpane) > 0);
	if (pane) throw new Error(`the column's "Checking sync…" pane showed at ${pane.t}ms: the check ran only once the column existed`);
	const both = frames(recording).find(({ t, state }) => t >= from && Number(state.synchero) > 0 && Number(state.rows) > 0);
	if (both) throw new Error(`transcript rows painted at ${both.t}ms while the hero still said it was syncing`);
}

await runJourney({
	name: 'mirrored-chat',
	recorder: { selectors: ['syncpane=.mirror-sync', 'synchero=.cleared-hero.is-loading', 'rows=[data-ui-row]'] },
	cases: [
		{
			name: 'warm-cursor-through-freshness',
			async setup(ctx) {
				await fromNew('long')(ctx);
				await openFromSidebar(ctx.page, SEEDED.long);
				await waitChatOpen(ctx.page, { messagesAtLeast: 50 });
				await ctx.page.waitForFunction(() => window.__mirrorSyncPolls > 2);
				await openFromSidebar(ctx.page, SEEDED.no_project);
				await waitChatOpen(ctx.page);
				await ctx.page.evaluate(() => { window.__mirrorSyncPolls = 0; window.__mirrorReaders = []; });
			},
			async act({ page, mark }) {
				await mark(OPEN);
				await openFromSidebar(page, SEEDED.long);
				await page.waitForFunction(() => window.__mirrorSyncPolls > 2);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				const readers = await page.evaluate(() => window.__mirrorReaders);
				expect(readers.length === 1 && readers[0] > 0,
					`warm mirror must keep one cursor-resume reader through freshness, got ${JSON.stringify(readers)}`);
			},
			verify({ recording }) {
				expectNeverDecreases(recording, 'rows', { from: markTime(recording, OPEN) });
			}
		},
		{
			name: 'long-from-new',
			setup: fromNew('long'),
			async act({ page, mark }) {
				await mark(OPEN);
				await openFromSidebar(page, SEEDED.long);
				await expectSyncingHero(page);
				await expectHeaderMatchesHero(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				const polls = await syncPolls(page);
				expect(polls > SYNCING_POLLS, `the owner's sync status was polled until caught up (${polls} polls)`);
			},
			verify({ recording }) {
				expectComposerPositions(recording, ['centre', 'bottom']);
				expectSyncedInTheHero(recording);
				// The transcript arrives once and never empties again.
				expectNeverDecreases(recording, 'rows', { from: markTime(recording, OPEN) });
			}
		},
		{
			// An untitled mirror (its row and /live carry no title yet): the
			// header and the hero both say "Loading…" until it is revealed.
			name: 'untitled-from-new',
			setup: fromNew('long', { untitled: true }),
			async act({ page, mark }) {
				await mark(OPEN);
				// The sidebar lists an untitled chat as "Untitled".
				await openFromSidebar(page, 'Untitled');
				await expectSyncingHero(page);
				await expectHeaderMatchesHero(page, 'Loading…');
				await waitChatOpen(page, { messagesAtLeast: 50 });
			},
			verify({ recording }) {
				expectComposerPositions(recording, ['centre', 'bottom']);
				expectSyncedInTheHero(recording);
			}
		},
		{
			// From another chat (the message column), #813: the transcript
			// stays mounted under the sync pane (no remount anomaly, no allow
			// rule) and the composer never leaves the bottom.
			name: 'long-from-chat',
			async setup(ctx) {
				await fromNew('long')(ctx);
				await openFromSidebar(ctx.page, SEEDED.no_project);
				await waitChatOpen(ctx.page);
			},
			async act({ page, mark }) {
				await mark(OPEN);
				await openFromSidebar(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				const polls = await syncPolls(page);
				expect(polls > SYNCING_POLLS, `the owner's sync status was polled until caught up (${polls} polls)`);
			},
			verify({ recording }) {
				expectComposerPositions(recording, ['bottom']);
			}
		},
		{
			name: 'cleared-from-new',
			setup: fromNew('cleared'),
			async act({ page, mark }) {
				await mark(OPEN);
				await openFromSidebar(page, SEEDED.cleared);
				await expectSyncingHero(page);
				await waitChat(page, { surface: 'cleared', loading: 'false', composer: 'centre', session: /^s:/ }, { timeout: 30_000 });
				await page.getByText('Conversation cleared', { exact: false }).waitFor();
				await page.waitForTimeout(1500);
				const polls = await syncPolls(page);
				expect(polls > SYNCING_POLLS, `an empty mirror is checked too (${polls} polls)`);
			},
			verify({ recording }) {
				expectComposerPositions(recording, ['centre']);
				expectSyncedInTheHero(recording);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
