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
//                        after a reload; a saved draft holding an @ opens
//                        nothing by itself; a pick inserts its relative path
//   typed-before-metadata the @ is typed while nothing has named the reloaded
//                        chat's project; the menu opens when the details
//                        arrive, with no further keystroke
//   worker-offline       the worker cannot answer: the menu says so, keeps the
//                        Quick Access folders, and Try again recovers
//   selected-file-and-upload a picked file and a real attachment in one message:
//                        the pick keeps its relative path, so it is no upload
//   selected-file-sends-after-reload a pick on its own, restored from the saved
//                        draft of a chat that already has a turn
//   remote-chat-keeps-local-agents a chat on another worker still holding its
//                        local project lists that project's agents
//   note-pick-names-the-note a note picked in a chat on another worker is
//                        inserted as `Notes/<id>`, not the viewing worker's
//                        file path (#1364)
//   note-pick-same-worker-keeps-path a note picked in a chat on this worker
//                        keeps this worker's file path, and the model is sent
//                        exactly that path
//
// Slow desktop and phone only. Shards have little time to spare, and the races
// here show up under the slow throttle.
import { runJourney } from './runner.mjs';
import { readFileSync } from 'node:fs';
import { PROJECT, SEEDED, api, composer, expect, gotoApp, isPhone, openFromSidebar, openNewChat, seededChatId, sendButton, showSidebar, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const PEER = 'Peer worker';
const ON_PEER = 'ONLY_ON_RUNNING_WORKER.md';
const OFFLINE = 'The worker running this chat is offline or unreachable.';
const MENU = '.file-autocomplete-panel';
const CHIP = '.ProseMirror [data-file-chip]';
const REFUSED = 'Message not sent';

/**
 * Make the page see seeded chat `key` as running on PEER, and script the file
 * list that worker answers with. In the page, not page.route: the built app's
 * service worker makes the requests page.route would see.
 */
async function simulateRemoteChat(page, status, key, { offline = false, keepLocalId = false } = {}) {
	await page.addInitScript(
		({ id, peer, file, offline, offlineDetail, keepLocalId }) => {
			const original = window.fetch.bind(window);
			// typed-before-metadata: nothing may name this chat's project until the
			// journey says so. The tab's saved snapshot would, at once, so it is put
			// back to what a chat saved without a project holds (the chat lane, or
			// the device's default project would be filled in). The session list
			// and /live are held below.
			const holding = !!sessionStorage.getItem('journey:holdMetadata');
			let releaseMetadata = () => {};
			const metadata = new Promise((done) => { releaseMetadata = done; });
			window.__liveHeld = 0;
			window.__releaseMetadata = () => {
				sessionStorage.removeItem('journey:holdMetadata');
				releaseMetadata();
			};
			if (holding) {
				const saved = JSON.parse(sessionStorage.getItem('ai-shuffle-active-session'));
				sessionStorage.setItem('ai-shuffle-active-session', JSON.stringify({ ...saved, mode: 'chat', project: '', projectV2Id: null }));
			}
			window.__fileLists = [];
			window.__subagentLists = [];
			const local = () => (keepLocalId ? {} : { project_id: null });
			window.__workerOffline = offline;
			const urlOf = (input) => new URL(typeof input === 'string' ? input : input.url, location.href);
			const markRemote = (value) => {
				if (Array.isArray(value)) return value.map(markRemote);
				if (!value || typeof value !== 'object') return value;
				const out = {};
				for (const [k, v] of Object.entries(value)) out[k] = markRemote(v);
				if (out.id === id) Object.assign(out, { is_remote: true, device_name: peer, ...local() });
				return out;
			};
			const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
			window.fetch = async (input, init) => {
				const url = urlOf(input);
				const method = (init?.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
				if (holding && method === 'GET') {
					const isLive = url.pathname === `/api/v1/sessions/${id}/live`;
					if (isLive) window.__liveHeld++;
					if (isLive || url.pathname === '/api/v1/sessions/cursor') await metadata;
				}
				if (/\/subagents\/all$/.test(url.pathname)) window.__subagentLists.push(url.searchParams.get('project_id'));
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
					Object.assign(patched, { ...local(), is_remote: true, device_name: peer });
				}
				return new Response(JSON.stringify(patched), { status: response.status, headers: response.headers });
			};
		},
		{ id: seededChatId(status, key), peer: PEER, file: ON_PEER, offline, offlineDetail: OFFLINE, keepLocalId }
	);
}

const fileLists = (page) => page.evaluate(() => window.__fileLists ?? []);

async function openChat(ctx, title) {
	if (isPhone(ctx.variant)) await showSidebar(ctx.page);
	await openFromSidebar(ctx.page, title);
	await waitChatOpen(ctx.page);
}

const start = (key, title, opts) => async (ctx) => {
	await simulateRemoteChat(ctx.page, ctx.status, key, opts);
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

/** Real keystrokes: type the name, take the highlighted row with Enter. */
async function pickReadme(page) {
	await composer(page).click();
	await page.keyboard.type('@README');
	const menu = page.locator(MENU);
	await menu.getByText('README.md').waitFor({ timeout: 15_000 });
	await page.keyboard.press('Enter');
	await menu.waitFor({ state: 'detached', timeout: 5_000 });
	await page.locator(CHIP).first().waitFor({ timeout: 5_000 });
}

/** What the model was sent for the message carrying `token`. */
function promptWith(status, token) {
	return readFileSync(status.fake_ai.request_log, 'utf8').split('\n').filter((line) => line.includes(token)).pop() ?? '';
}

/** Send with the button, then require a reply rather than a refusal. */
async function sendAndExpectReply(page, status, token, messagesAtLeast) {
	await sendButton(page).click();
	await waitTurnSettled(page, { messagesAtLeast, timeout: 30_000 });
	expect(await page.getByText(REFUSED, { exact: false }).count() === 0, 'the message was not refused');
	const prompt = promptWith(status, token);
	expect(prompt, `the model received the message carrying ${token}`);
	return prompt;
}

/** Real keystrokes: open Notes, find the note by its token, take it with Enter. */
async function pickNote(page, { title, token }) {
	await composer(page).click();
	await page.keyboard.type(`@Notes/${token}`);
	const menu = page.locator(MENU);
	await menu.getByText(title).waitFor({ timeout: 15_000 });
	await page.keyboard.press('Enter');
	await menu.waitFor({ state: 'detached', timeout: 5_000 });
	const chip = page.locator(CHIP).first();
	await chip.waitFor({ timeout: 5_000 });
	return chip.getAttribute('data-path');
}

/** A note of this run's own, found in the menu by its token. */
async function stageNote(page) {
	const token = uniq();
	const title = `Mention note ${token}`;
	const note = await api(page, 'POST', '/notes', { title, content: `Note body ${token}` });
	return { id: note.id, title, token };
}

const startNewChat = async ({ page, status }) => { await openNewChat({ page, status }, { project: PROJECT }); };

await runJourney({
	name: 'remote-chat-mentions',
	throttles: ['slow'],
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
				await draftSaved(page);

				await mark('reload with @ in the saved draft');
				await page.reload({ waitUntil: 'domcontentloaded' });
				// Its own step: leaving the old document is not the app going backwards.
				await nextFrames(page);
				await mark('page reloaded');
				await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast: 1 }, { timeout: 60_000 });

				await composer(page).getByText('@').waitFor({ timeout: 10_000 });
				await page.waitForTimeout(1500);
				expect(await page.locator(MENU).count() === 0, 'a restored draft opens no menu by itself');
				await clearComposer(page);

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
				await page.evaluate(() => sessionStorage.setItem('journey:holdMetadata', '1'));
				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await nextFrames(page);
				await mark('page reloaded');
				// The details are asked for, and the gate has not let them through.
				await page.waitForFunction(() => window.__liveHeld > 0, undefined, { timeout: 30_000 });

				await mark('type @ while the chat details are held');
				await typeAt(page);
				await composer(page).getByText('@').waitFor({ timeout: 10_000 });
				// Long enough for a menu that should not be there to show itself.
				await page.waitForTimeout(1000);
				expect(await page.locator(MENU).count() === 0, 'no menu before anything names the project');
				expect((await fileLists(page)).length === 0, 'no file list is asked for before anything names the project');

				await mark('release the chat details, with no further keystroke');
				await page.evaluate(() => window.__releaseMetadata());
				await expectRunningWorkerFiles(page);
				expect((await fileLists(page)).length > 0, 'the file list is asked for once the details arrive');

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
			name: 'selected-file-sends-after-reload',
			setup: startNewChat,
			async act({ page, mark, status }) {
				await mark('a first turn, so the chat exists');
				await typeAndSend(page, `hello ${uniq()}`);
				await waitTurnSettled(page, { messagesAtLeast: 2 });
				await mark('take the file and let the draft save');
				await pickReadme(page);
				await draftSaved(page);
				await mark('reload');
				await page.reload({ waitUntil: 'domcontentloaded' });
				await nextFrames(page);
				await mark('page reloaded');
				await waitChat(page, { surface: 'active', loading: 'false', session: /^s:/, messagesAtLeast: 2 }, { timeout: 60_000 });
				await page.locator(CHIP).first().waitFor({ timeout: 10_000 });
				const token = uniq();
				// Focus without a tap: on a phone the tap lands on the chip, selecting it.
				await composer(page).press('ControlOrMeta+End');
				await page.keyboard.type(` again ${token}`);
				await mark('send the restored chip');
				const prompt = await sendAndExpectReply(page, status, token, 4);
				expect(prompt.includes('@README.md'), `the restored chip still names the file, got ${prompt.slice(0, 400)}`);
			}
		},
		{
			name: 'selected-file-and-upload',
			setup: startNewChat,
			async act({ page, mark, status }) {
				const token = uniq();
				const name = `attached-${token}.txt`;
				await mark('take the file');
				await pickReadme(page);
				await mark('attach a real file');
				await page.locator('input[type="file"]').first().setInputFiles({ name, mimeType: 'text/plain', buffer: Buffer.from('attached body') });
				await page.locator(CHIP).nth(1).waitFor({ timeout: 15_000 });
				// Focus without a tap: on a phone the tap lands on the chip, selecting it.
				await composer(page).press('ControlOrMeta+End');
				await page.keyboard.type(` both ${token}`);
				await mark('send both');
				const prompt = await sendAndExpectReply(page, status, token, 2);
				expect(prompt.includes('@README.md'), `the pick keeps its relative path, got ${prompt.slice(0, 400)}`);
				expect(prompt.includes(`/${name}`) || prompt.includes(`\\\\${name}`), `the attachment keeps its full path, got ${prompt.slice(0, 400)}`);
			}
		},
		{
			name: 'remote-chat-keeps-local-agents',
			setup: start('long', SEEDED.long, { keepLocalId: true }),
			async act({ page, mark }) {
				await mark('open Subagents in a chat on another worker that still holds its local project');
				await composer(page).click();
				await page.keyboard.type('@Subagents/');
				await page.waitForFunction(() => window.__subagentLists.length > 0, undefined, { timeout: 15_000 });
				const asked = await page.evaluate(() => window.__subagentLists);
				expect(asked.every((id) => id && !id.startsWith('pv2-')), `agents are listed for the local project, got ${JSON.stringify(asked)}`);
				await clearComposer(page);
				await draftSaved(page);
			}
		},
		{
			name: 'note-pick-names-the-note',
			setup: async (ctx) => {
				await start('long', SEEDED.long, {})(ctx);
				ctx.note = await stageNote(ctx.page);
			},
			async act({ page, mark, note }) {
				await mark('pick a note in a chat on another worker');
				const path = await pickNote(page, note);
				expect(path === `Notes/${note.id}`, `the chip names the note, not this worker's file, got ${path}`);
				await clearComposer(page);
				await draftSaved(page);
			}
		},
		{
			name: 'note-pick-same-worker-keeps-path',
			setup: async (ctx) => {
				await startNewChat(ctx);
				ctx.note = await stageNote(ctx.page);
			},
			async act({ page, mark, status, note }) {
				await mark('take the note in a new chat on this worker');
				const path = await pickNote(page, note);
				expect(/[\\/]notes[\\/][^/\\]+\.md$/.test(path ?? ''), `the chip keeps this worker's note file, got ${path}`);
				const token = uniq();
				// Focus without a tap: on a phone the tap lands on the chip, selecting it.
				await composer(page).press('ControlOrMeta+End');
				await page.keyboard.type(` read ${token}`);
				await mark('send it');
				const prompt = await sendAndExpectReply(page, status, token, 2);
				expect(prompt.includes(JSON.stringify(`@${path}`).slice(1, -1)), `the model gets that same path, got ${prompt.slice(0, 400)}`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
