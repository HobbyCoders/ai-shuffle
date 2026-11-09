// `@` file mentions in a chat that runs on another worker.
//
// Such a chat's row carries only the shared project id: `project_id` is null,
// `project_v2_id` is set. Bug this guards (#1330): after a reload the composer
// opened no menu and sent no request, because it required a local project.
//
// One dev instance has no second worker, so the page sees a seeded chat as
// remote: its session rows drop the local project id and name a peer, and the
// file list "from that worker" is scripted. The list must be asked for with
// the chat's session id, which is what lets the server ask the running worker.
// The two-worker path behind that request is checked by hand against a paired
// dev instance and by tests/api/test_session_files.py.
//
// Cases:
//   lists-after-reload   the menu shows the running worker's files, before and
//                        after a reload; a pick inserts its relative path
//   typed-before-metadata the @ is typed while the reloaded chat's details are
//                        still loading; the menu opens when they arrive
//   restored-draft-stays-closed a saved draft holding an @ opens nothing by
//                        itself on reload; only typing does
//   worker-offline       the worker cannot answer: the menu says so, keeps the
//                        Quick Access folders, and Try again recovers
//   local-chat-unchanged a chat on this worker still lists its own files
//   no-project-no-menu   a chat with no project at all opens no menu
import { runJourney } from './runner.mjs';
import { SEEDED, composer, expect, gotoApp, isPhone, openFromSidebar, seededChatId, showSidebar, waitChat, waitChatOpen } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const PEER = 'Peer worker';
const ON_PEER = 'ONLY_ON_RUNNING_WORKER.md';
const OFFLINE = 'The worker running this chat is offline or unreachable.';
const MENU = '.file-autocomplete-panel';

/**
 * Make the page see seeded chat `key` as running on PEER, and script the file
 * list that worker answers with. In the page, not page.route: the built app's
 * service worker makes the requests page.route would see.
 */
async function simulateRemoteChat(page, status, key, { offline = false } = {}) {
	await page.addInitScript(
		({ id, peer, file, offline, offlineDetail }) => {
			const original = window.fetch.bind(window);
			window.__fileLists = [];
			window.__workerOffline = offline;
			const urlOf = (input) => new URL(typeof input === 'string' ? input : input.url, location.href);
			const markRemote = (value) => {
				if (Array.isArray(value)) return value.map(markRemote);
				if (!value || typeof value !== 'object') return value;
				const out = {};
				for (const [k, v] of Object.entries(value)) out[k] = markRemote(v);
				if (out.id === id) Object.assign(out, { is_remote: true, device_name: peer, project_id: null });
				return out;
			};
			const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
			window.fetch = async (input, init) => {
				const url = urlOf(input);
				const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
				if (url.pathname === `/api/v1/sessions/${id}/live` && sessionStorage.getItem('journey:delayLive')) {
					await new Promise((done) => setTimeout(done, 1500));
				}
				if (url.pathname === `/api/v1/sessions/${id}/sync-status`) {
					return json({ state: 'current', scope: 'session', initial_caught_up: true, pending_updates: 0 });
				}
				if (/^\/api\/v1\/projects\/[^/]+\/files$/.test(url.pathname) && url.searchParams.get('session_id') === id) {
					window.__fileLists.push(url.pathname.split('/')[4]);
					if (window.__workerOffline) return json({ detail: offlineDetail }, 503);
					return json({ files: [{ name: file, type: 'file', size: 12, path: file }], path: '', projectId: 'shared' });
				}
				const response = await original(input, init);
				if (method !== 'GET' || !response.ok || !url.pathname.startsWith('/api/v1/sessions') || /\/(events|stream)$/.test(url.pathname)) return response;
				if (!(response.headers.get('content-type') || '').includes('application/json')) return response;
				const body = await response.clone().json().catch(() => undefined);
				if (body === undefined) return response;
				// /live describes the open chat without repeating its id.
				const patched = markRemote(body);
				if (url.pathname === `/api/v1/sessions/${id}/live` && patched && typeof patched === 'object' && 'project_id' in patched) {
					Object.assign(patched, { project_id: null, is_remote: true, device_name: peer });
				}
				return new Response(JSON.stringify(patched), { status: response.status, headers: response.headers });
			};
		},
		{ id: seededChatId(status, key), peer: PEER, file: ON_PEER, offline, offlineDetail: OFFLINE }
	);
}

const fileLists = (page) => page.evaluate(() => window.__fileLists ?? []);

async function openChat(ctx, title) {
	if (isPhone(ctx.variant)) await showSidebar(ctx.page);
	await openFromSidebar(ctx.page, title);
	await waitChatOpen(ctx.page);
}

const start = (key, title, opts) => async (ctx) => {
	if (opts) await simulateRemoteChat(ctx.page, ctx.status, key, opts);
	await gotoApp(ctx.page, ctx.status);
	await waitChat(ctx.page, { surface: 'new' });
	await openChat(ctx, title);
};

/** Real keystrokes, the way a person starts a mention. */
async function typeAt(page) {
	await composer(page).click();
	await page.keyboard.type('@');
}

/** Empty the composer so the next `@` starts a fresh mention. */
async function clearComposer(page) {
	await composer(page).click();
	await page.keyboard.press('ControlOrMeta+a');
	await page.keyboard.press('Backspace');
}

/** Past the composer's draft debounce, so a reload finds what is there now. */
const draftSaved = (page) => page.waitForTimeout(1500);

const nextFrames = (page) => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));

async function expectRunningWorkerFiles(page) {
	const menu = page.locator(MENU);
	await menu.getByText(ON_PEER).waitFor({ timeout: 15_000 });
	for (const folder of ['Chat History', 'Notes', 'Subagents']) {
		await menu.getByText(folder, { exact: false }).first().waitFor({ timeout: 5_000 });
	}
}

await runJourney({
	name: 'remote-chat-mentions',
	cases: [
		{
			name: 'lists-after-reload',
			setup: start('long', SEEDED.long, {}),
			async act(ctx) {
				const { page, mark } = ctx;
				await mark('type @ before a reload');
				await typeAt(page);
				await expectRunningWorkerFiles(page);
				const shared = await fileLists(page);
				expect(shared.length > 0 && shared.every((id) => id.startsWith('pv2-')),
					`the list is asked for by the shared project id, got ${JSON.stringify(shared)}`);
				await clearComposer(page);
				await page.locator(MENU).waitFor({ state: 'detached', timeout: 5_000 });
				await draftSaved(page);

				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				// Its own step: leaving the old document is not the app going backwards.
				await nextFrames(page);
				await mark('page reloaded');
				await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast: 1 }, { timeout: 60_000 });

				await mark('type @ after the reload');
				await typeAt(page);
				await expectRunningWorkerFiles(page);

				await mark('pick the file');
				await page.locator(MENU).getByText(ON_PEER).click();
				await page.locator(MENU).waitFor({ state: 'detached', timeout: 5_000 });
				await page.locator('.ProseMirror').getByText(ON_PEER).first().waitFor({ timeout: 5_000 });
				await clearComposer(page);
				await draftSaved(page);
			}
		},
		{
			name: 'typed-before-metadata',
			setup: start('long', SEEDED.long, {}),
			async act({ page, mark }) {
				await page.evaluate(() => sessionStorage.setItem('journey:delayLive', '1'));
				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await nextFrames(page);
				await mark('type @ while the chat details load');
				await typeAt(page);
				await expectRunningWorkerFiles(page);
				await page.evaluate(() => sessionStorage.removeItem('journey:delayLive'));
				await clearComposer(page);
				await draftSaved(page);
			}
		},
		{
			name: 'restored-draft-stays-closed',
			setup: start('long', SEEDED.long, {}),
			async act({ page, mark }) {
				await typeAt(page);
				await page.locator(MENU).getByText(ON_PEER).waitFor({ timeout: 15_000 });
				await draftSaved(page);
				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await nextFrames(page);
				await mark('page reloaded');
				await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast: 1 }, { timeout: 60_000 });
				await composer(page).getByText('@').waitFor({ timeout: 10_000 });
				await page.waitForTimeout(1500);
				expect(await page.locator(MENU).count() === 0, 'a restored draft opens no menu by itself');
				await clearComposer(page);
				await draftSaved(page);
			}
		},
		{
			name: 'worker-offline',
			setup: start('long', SEEDED.long, { offline: true }),
			allow: [{ kind: 'failed_requests', status: 503, url: /\/files\?/, reason: 'this case scripts the offline worker it tests' }],
			async act({ page, mark }) {
				await mark('type @ while the worker is offline');
				await typeAt(page);
				const menu = page.locator(MENU);
				await menu.getByText(OFFLINE).waitFor({ timeout: 15_000 });
				await menu.getByText('Notes', { exact: false }).first().waitFor({ timeout: 5_000 });
				expect(await menu.locator('.autocomplete-loading').count() === 0, 'no spinner once the failure is known');

				await mark('try again once the worker is back');
				await page.evaluate(() => { window.__workerOffline = false; });
				await menu.getByRole('button', { name: 'Try again' }).click();
				await menu.getByText(ON_PEER).waitFor({ timeout: 15_000 });
				expect(await menu.getByText(OFFLINE).count() === 0, 'the notice goes away once the list loads');
				await clearComposer(page);
			}
		},
		{
			name: 'local-chat-unchanged',
			setup: start('long', SEEDED.long),
			async act({ page, mark }) {
				await mark('type @ in a chat on this worker');
				await typeAt(page);
				const menu = page.locator(MENU);
				await menu.getByText('README.md').waitFor({ timeout: 15_000 });
				await clearComposer(page);
			}
		},
		{
			name: 'no-project-no-menu',
			setup: start('no_project', SEEDED.no_project),
			async act({ page, mark }) {
				await mark('type @ in a chat with no project');
				await typeAt(page);
				await page.waitForTimeout(1000);
				expect(await page.locator(MENU).count() === 0, 'a chat with no project opens no file menu');
				await clearComposer(page);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
