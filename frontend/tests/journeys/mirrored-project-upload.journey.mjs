// Journey: where a composer upload goes in a project chat another worker runs
// (#1659).
//
// Legacy project ids are per worker. This worker's copy of another worker's
// chat keeps that worker's id whenever a project with the same id exists here,
// related or not. The page must then hold the shared project id alone, so an
// upload goes through the session upload (the server routes it to the owner)
// and never into the local project's folder. Until the page knows who runs the
// chat (a reload, `/live` slow or failing), no upload may go to the local
// folder at all; and when the owner changes while the chat is open (a
// hand-off), the next upload follows the new owner.
//
// One dev instance has no second worker, so the page sees the seeded
// "Other project chat" (project dev-other, which exists here: the collision)
// as run elsewhere: its session rows and /live say `is_remote`, the owner's
// sync status is scripted caught up, and uploads are answered in the page and
// recorded, so nothing is written. A hand-off is the server telling the page
// its sessions changed (`resources_changed`) with rows that now name the other
// owner; the move itself between two real workers is not driven here (it
// needs a second worker; see the PR for the manual two-worker run).
//
// Cases:
//   switch-away-and-back  open the mirrored chat, open another chat, come back:
//                         the upload goes through the session upload
//   reload-old-snapshot   a reload restores a snapshot naming the local id while
//                         /live is held and no row says who runs it: the picked file waits, then
//                         goes through the session upload once the owner is known
//   live-fails            /live fails and no row says who runs the chat: the
//                         upload is refused with a reason, nothing is sent
//   handoff-while-open    a local project chat is handed to another worker while
//                         open: the first upload goes to the local project, the
//                         next one through the session upload
import { runJourney } from './runner.mjs';
import { SEEDED, expect, gotoApp, openFromSidebar, seededChatId, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const PEER = 'Peer worker';
const LOCAL_PROJECT = 'dev-other';
const SNAPSHOT_KEY = 'ai-shuffle-active-session';

/**
 * Script the page before it loads. In the page, not page.route: the built
 * app's service worker makes the requests page.route would see.
 *   remote      rows and /live say another worker runs chat `id` (window.__remote flips it)
 *   hideOwner   its rows carry no `is_remote` (only /live can say who runs it)
 *   failLive    its /live answers 500
 *   holdReads   after the armed reload, its /live waits until window.__releaseLive()
 */
async function scriptPage(page, status, { remote = true, hideOwner = false, failLive = false, holdReads = false, plantSnapshot = false } = {}) {
	await page.addInitScript(
		({ id, peer, remote, hideOwner, failLive, holdReads, plantSnapshot, snapshotKey, localProject }) => {
			window.__remote = remote;
			window.__uploads = window.__uploads || [];
			window.__sockets = [];
			window.__syncPolls = 0;
			// Armed by the journey for the next load (the reload), not the first one.
			const armed = sessionStorage.getItem('__journeyPlant') === 'yes';
			// A snapshot an older page saved: the local id beside the shared one.
			if (plantSnapshot && armed) {
				sessionStorage.removeItem('__journeyPlant');
				try {
					const saved = JSON.parse(sessionStorage.getItem(snapshotKey) || '{}');
					if (saved.sessionId === id) {
						sessionStorage.setItem(snapshotKey, JSON.stringify({ ...saved, project: localProject }));
						window.__planted = true;
					}
				} catch { /* no snapshot to plant into */ }
			}
			const startedAt = performance.now();
			const NativeSocket = window.WebSocket;
			window.WebSocket = class extends NativeSocket {
				constructor(...args) { super(...args); window.__sockets.push(this); }
			};
			const original = window.fetch.bind(window);
			const pathOf = (input) => {
				try { return new URL(typeof input === 'string' ? input : input.url, location.href).pathname; } catch { return ''; }
			};
			const mark = (value) => {
				if (Array.isArray(value)) return value.map(mark);
				if (!value || typeof value !== 'object') return value;
				const out = {};
				for (const [k, v] of Object.entries(value)) out[k] = mark(v);
				if (out.id === id) {
					if (hideOwner) delete out.is_remote;
					else if (window.__remote) Object.assign(out, { is_remote: true, device_name: peer });
				}
				if (out.session_meta && window.__remote) Object.assign(out.session_meta, { is_remote: true, device_name: peer });
				return out;
			};
			const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
			window.fetch = async (input, init) => {
				const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
				const path = pathOf(input);
				if (method === 'POST' && /\/upload$/.test(path)) {
					window.__uploads.push({ path, at: performance.now() - startedAt });
					return json({ path: 'uploads/note.txt', filename: 'note.txt', full_path: `/journey${path}/note.txt`, size: 4 });
				}
				// The owner's sync check: syncing twice after each reset, then caught
				// up, so the check is a real state rather than a one-frame flash.
				if (path === `/api/v1/sessions/${id}/sync-status`) {
					window.__syncPolls += 1;
					const target = { seq: 1, epoch: 'simulated', tail_id: 'simulated' };
					return json(window.__syncPolls <= 2
						? { state: 'syncing', scope: 'session', initial_target: target, initial_caught_up: false, initial_pending_updates: 3, pending_updates: 3 }
						: { state: 'current', scope: 'session', initial_caught_up: true, pending_updates: 0 });
				}
				const isLive = path === `/api/v1/sessions/${id}/live`;
				if (holdReads && armed && isLive && !window.__liveReleased) {
					window.__liveHeld = (window.__liveHeld || 0) + 1;
					// Every held read is released together (the page may retry meanwhile).
					await new Promise(done => {
						const waiting = window.__liveWaiting = window.__liveWaiting || [];
						waiting.push(done);
						window.__releaseLive = () => { window.__liveReleased = true; for (const release of waiting.splice(0)) release(); };
					});
				}
				if (failLive && isLive) return new Response('unavailable', { status: 500 });
				const response = await original(input, init);
				if (method !== 'GET' || !response.ok || !path.startsWith('/api/v1/sessions') || /\/(events|stream)$/.test(path)) return response;
				if (!(response.headers.get('content-type') || '').includes('application/json')) return response;
				const body = await response.clone().json().catch(() => undefined);
				if (body === undefined) return response;
				return new Response(JSON.stringify(mark(body)), { status: response.status, headers: response.headers });
			};
		},
		{ id: seededChatId(status, 'other_project'), peer: PEER, remote, hideOwner, failLive, holdReads, plantSnapshot, snapshotKey: SNAPSHOT_KEY, localProject: LOCAL_PROJECT }
	);
}

/** Pick a file through the composer's own file input (as the attach button's picker does). */
async function pickFile(page) {
	await page.locator('input[type="file"]').first().setInputFiles({ name: 'note.txt', mimeType: 'text/plain', buffer: Buffer.from('note') });
}

const uploads = (page) => page.evaluate(() => window.__uploads.map(u => u.path));

async function expectUploads(page, chatId, expected) {
	await page.waitForFunction((n) => window.__uploads.length >= n, expected.length, { timeout: 15_000 });
	await page.waitForTimeout(500);
	const got = await uploads(page);
	const want = expected.map(kind => kind === 'session' ? `/api/v1/sessions/${chatId}/upload` : `/api/v1/projects/${LOCAL_PROJECT}/upload`);
	expect(JSON.stringify(got) === JSON.stringify(want), `uploads went to ${JSON.stringify(got)}, expected ${JSON.stringify(want)}`);
}

async function openMirrored(page) {
	await openFromSidebar(page, SEEDED.other_project);
	await waitChatOpen(page);
}

await runJourney({
	name: 'mirrored-project-upload',
	cases: [
		{
			name: 'switch-away-and-back',
			async setup({ page, status }) {
				await scriptPage(page, status);
				await gotoApp(page, status);
				await openMirrored(page);
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page);
				await page.evaluate(() => { window.__syncPolls = 0; });
			},
			async act({ page, status, mark }) {
				await mark('back to the mirrored chat');
				await openMirrored(page);
				await mark('attach a file');
				await pickFile(page);
				await expectUploads(page, seededChatId(status, 'other_project'), ['session']);
			}
		},
		{
			name: 'reload-old-snapshot',
			async setup({ page, status }) {
				await scriptPage(page, status, { hideOwner: true, holdReads: true, plantSnapshot: true });
				await gotoApp(page, status);
				await openMirrored(page);
				await page.waitForTimeout(1000);
				await page.evaluate(() => sessionStorage.setItem('__journeyPlant', 'yes'));
				// The reload is setup: what is recorded is the window it leaves.
				await page.reload();
				// The composer is usable before /live answers: that is the window.
				await page.locator('[role=textbox][aria-label=Message]').first().waitFor({ timeout: 60_000 });
				await page.waitForFunction(() => window.__liveHeld > 0, null, { timeout: 30_000 });
				expect(await page.evaluate(() => window.__planted === true), 'the reload restored the planted snapshot naming the local id');
				await page.waitForTimeout(1000);
			},
			async act({ page, status, mark }) {
				await mark('attach a file while /live is held');
				await pickFile(page);
				// Owner not known yet (only /live can say, and it is held): nothing may leave.
				await page.waitForTimeout(1500);
				expect((await uploads(page)).length === 0, `an upload left before the owner was known: ${JSON.stringify(await uploads(page))}`);
				await mark('/live answers: another worker runs it');
				await page.evaluate(() => window.__releaseLive());
				await expectUploads(page, seededChatId(status, 'other_project'), ['session']);
			}
		},
		{
			name: 'live-fails',
			allow: [
				{ kind: 'failed_requests', status: 500, url: /\/live(\?|$)/, reason: 'this case scripts the failing /live it tests' },
				{ kind: 'errors', message: /Upload failed: .*couldn’t confirm which worker runs this chat/, reason: 'the refused upload this case expects logs its reason' }
			],
			async setup({ page, status }) {
				await scriptPage(page, status, { hideOwner: true, failLive: true });
				await gotoApp(page, status);
			},
			async act({ page, mark }) {
				await mark('open the chat whose owner nothing says');
				await openFromSidebar(page, SEEDED.other_project);
				await waitChat(page, { surface: 'active', session: /^s:/ }, { timeout: 60_000 });
				await mark('attach a file');
				await pickFile(page);
				await page.getByText(/couldn’t confirm which worker runs this chat/).first().waitFor({ timeout: 10_000 });
				expect((await uploads(page)).length === 0, 'nothing was uploaded while the owner stayed unknown');
			}
		},
		{
			name: 'handoff-while-open',
			async setup({ page, status }) {
				await scriptPage(page, status, { remote: false });
				await gotoApp(page, status);
				await openMirrored(page);
			},
			async act({ page, status, mark }) {
				const chatId = seededChatId(status, 'other_project');
				await mark('attach while this worker runs the chat');
				await pickFile(page);
				await expectUploads(page, chatId, ['project']);
				await mark('hand the chat to another worker');
				await page.evaluate(() => {
					window.__remote = true;
					window.__syncPolls = 0;
					const live = window.__sockets.filter(s => s.url.includes('/ws/global') && s.readyState === 1).at(-1);
					live.onmessage({ data: JSON.stringify({ type: 'resources_changed', resources: ['sessions'] }) });
				});
				await page.waitForTimeout(2000);
				await mark('attach after the hand-off');
				await pickFile(page);
				await expectUploads(page, chatId, ['project', 'session']);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
