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
//   unreachable-from-new  the sync check keeps failing (#814): after a few
//                      failures the chat shows what it has with a "Can't
//                      reach" notice, and Retry clears it once the owner answers
//   shared-project-header  #1659: the owner filed the chat under its own
//                      "dev-sandbox", which is another project here. The row
//                      names no local id, only the shared id of "Dev other":
//                      the header crumb and the Git panel name "Dev other",
//                      never the colliding local "Dev sandbox". The row is
//                      changed on the private database (as the viewer stores a
//                      mirror after #1659) and restored after; it is not marked
//                      remote, since Git status needs a worker that answers.
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { SEEDED, expect, gotoApp, openFromSidebar, seededChatId, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { expectComposerPositions, expectNeverDecreases, frames, markTime } from './lib/timeline.mjs';

const PEER = 'Peer worker';
/** The shared project the mirrored chat belongs to, and this worker's project that has the owner's slug. */
const OTHER_PROJECT = 'Dev other';
const LOCAL_COLLIDING_PROJECT = 'Dev sandbox';
const OPEN = 'open the mirrored chat from the sidebar';
/** "Syncing" answers before the owner reports the opening checkpoint caught up. */
const SYNCING_POLLS = 2;

/**
 * Make the page see seeded chat `key` as mirrored from PEER, and script its
 * sync status. In the page, not page.route: the built app's service worker
 * makes the requests page.route would see (see withoutSavedAgentPick). Call
 * before the page loads; `syncPolls(page)` reads how often it was polled.
 */
async function simulateMirroredChat(page, status, key, { untitled = false, failing = false } = {}) {
	await page.addInitScript(
		({ id, peer, syncingPolls, untitled, failing }) => {
			const original = window.fetch.bind(window);
			window.__mirrorSyncPolls = 0;
			// A failing check answers 503 until the journey sets this.
			window.__mirrorSyncHealthy = !failing;
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
					if (!window.__mirrorSyncHealthy) return new Response('unavailable', { status: 503 });
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
		{ id: seededChatId(status, key), peer: PEER, syncingPolls: failing ? 0 : SYNCING_POLLS, untitled, failing }
	);
}

// The viewer's stored copy of a chat whose owner filed it under its own
// "dev-sandbox" (another project here): no local id, only the shared id of
// "dev-other" (#1659). On the private database only; 'restore' undoes it.
const SHARED_PROJECT_ROW = String.raw`
import os, sys
from pathlib import Path
home = Path(sys.argv[1]).resolve()
assert home.name == 'data' and home.parent.name.startswith('ai-shuffle-preview-')
assert (home / '.branch-preview').exists() or (home.parent / '.branch-preview').exists()
os.environ['DATA_DIR'] = str(home)
from app.core.config import settings
assert settings.effective_data_dir.resolve() == home
import psycopg2
from psycopg2.extras import RealDictCursor
from app.core.project_resolver import origin_for_legacy_project
from app.db.projects_v2 import project_v2_id_for_origin
chat, stage = sys.argv[2:4]
with psycopg2.connect(**settings.get_database_connection()) as conn:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        slug = 'dev-other' if stage == 'shared' else 'dev-sandbox'
        cur.execute('SELECT id, link_group_id FROM projects WHERE id = %s', (slug,))
        shared = project_v2_id_for_origin(origin_for_legacy_project(dict(cur.fetchone())))
        cur.execute('UPDATE sessions SET project_id = %s, project_v2_id = %s WHERE id = %s',
                    (None if stage == 'shared' else slug, shared, chat))
        assert cur.rowcount == 1
`;

function fileUnderSharedProject(status, sessionId, stage) {
	const local = join(status.checkout, '.venv', 'bin', 'python');
	const python = existsSync(local) ? local : (process.env.PYTHON || 'python3');
	execFileSync(python, ['-c', SHARED_PROJECT_ROW, status.data_dir, sessionId, stage], {
		cwd: status.checkout,
		env: { ...process.env, PYTHONPATH: join(status.checkout, 'fluid'), PYTHONDONTWRITEBYTECODE: '1' },
		encoding: 'utf8'
	});
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
		},
		{
			name: 'shared-project-header',
			async setup(ctx) {
				const { page, status } = ctx;
				fileUnderSharedProject(status, seededChatId(status, 'long'), 'shared');
				await gotoApp(page, status);
				await waitChat(page, { surface: 'new' });
				await page.waitForTimeout(1000);
			},
			async act({ page, mark, variant }) {
				await mark(OPEN);
				await openFromSidebar(page, SEEDED.long);
				await waitChatOpen(page, { messagesAtLeast: 50 });
				const crumb = page.locator('.crumb.project:visible');
				if (variant.viewport === 'desktop') {
					await crumb.first().waitFor({ timeout: 10_000 });
					const text = (await crumb.first().textContent())?.trim();
					expect(text === OTHER_PROJECT, `the header crumb names ${OTHER_PROJECT}, got ${JSON.stringify(text)}`);
				} else {
					expect(await crumb.count() === 0 || (await crumb.first().textContent())?.trim() === OTHER_PROJECT,
						'a phone header crumb, if any, names the shared project');
				}
				const gitButton = page.getByRole('button', { name: /^Git actions/ }).first();
				await gitButton.waitFor({ timeout: 15_000 });
				await gitButton.click();
				const project = page.locator('.git-panel .gp-project');
				await project.waitFor({ timeout: 10_000 });
				const label = (await project.textContent())?.trim();
				expect(label === `${OTHER_PROJECT} /`, `the Git panel names ${OTHER_PROJECT}, got ${JSON.stringify(label)}`);
				const named = await page.locator('.crumb.project:visible, .git-panel .gp-project').allTextContents();
				expect(!named.some((text) => text.includes(LOCAL_COLLIDING_PROJECT)),
					`neither header names the colliding local ${LOCAL_COLLIDING_PROJECT}: ${JSON.stringify(named)}`);
				await page.keyboard.press('Escape');
			},
			verify({ status }) {
				fileUnderSharedProject(status, seededChatId(status, 'long'), 'restore');
			}
		},
		{
			// #814: a sync check that keeps failing used to wait in the hero
			// forever. Now the chat opens with what it has and says why.
			name: 'unreachable-from-new',
			setup: fromNew('long', { failing: true }),
			allow: [{ kind: 'failed_requests', status: 503, url: /\/sync-status(\?|$)/, reason: 'this case scripts the failing sync check it tests' }],
			async act({ page, mark }) {
				await mark(OPEN);
				await openFromSidebar(page, SEEDED.long);
				await page.locator('.cleared-hero').getByText(`Checking sync with ${PEER}…`).waitFor({ timeout: 30_000 });
				await waitChatOpen(page, { messagesAtLeast: 50 });
				const notice = page.locator('.mirror-pill.offline', { hasText: `Can't reach ${PEER} right now` });
				await notice.waitFor({ timeout: 5_000 });
				const polls = await syncPolls(page);
				expect(polls >= 3 && polls <= 4, `the chat opened after a bounded number of failed checks (${polls} polls)`);
				// The owner answers from the Retry press on. The chat keeps re-checking
				// in the background (4s, then 8s, ...); those checks still fail until
				// the press, so a background check cannot clear the notice from under
				// the click. The checks are counted inside the press's own dispatch
				// (before the product's handler, then on window, last on the bubble
				// path), where no background timer can run: a Retry that did nothing
				// would leave the count as it was, whatever a later check does.
				await page.evaluate(() => {
					let press = null;
					document.addEventListener('click', (event) => {
						if (!(event.target instanceof Element) || !event.target.closest('.mirror-pill.offline .mirror-retry')) return;
						press = event;
						window.__mirrorSyncHealthy = true;
						window.__mirrorRetryPolls = { before: window.__mirrorSyncPolls };
					}, { capture: true });
					window.addEventListener('click', (event) => {
						if (event === press) window.__mirrorRetryPolls.after = window.__mirrorSyncPolls;
					});
				});
				await notice.getByRole('button', { name: 'Retry' }).click();
				const retry = await page.evaluate(() => window.__mirrorRetryPolls ?? null);
				expect(retry && retry.after === retry.before + 1,
					`the Retry press itself checked the owner once (${retry ? `${retry.before} polls before the press, ${retry.after} after it` : 'the press was not seen'})`);
				await notice.waitFor({ state: 'detached', timeout: 5_000 });
			},
			verify({ recording }) {
				expectComposerPositions(recording, ['centre', 'bottom']);
				expectSyncedInTheHero(recording);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
