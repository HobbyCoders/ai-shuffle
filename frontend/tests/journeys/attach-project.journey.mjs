// Journey: attaching a project to a chat that started without one.
//
// Cases:
//   rewound-attachment-resends    (#1653) a message with an attached file is
//                                 sent in a chat with no project, a project is
//                                 attached from the "No project" pill, then the
//                                 message is rewound and sent again: the turn
//                                 runs and the file reaches the AI again.
//   late-success-after-switch     (#1663) the sheet is open with a project
//                                 picked, Attach is pressed and the page moves to
//                                 another chat before the answer: the sheet is
//                                 gone there, and its late answer does not bring
//                                 it back. Reopened in that chat, nothing is
//                                 picked.
//   late-error-after-switch       (#1663) the same with an answer that fails:
//                                 its error is not shown in the other chat's
//                                 sheet.
import { readFile } from 'node:fs/promises';
import { fakeAiTag, runJourney } from './runner.mjs';
import { PROJECT, api, composer, expect, openFromSidebar, openNewChat, pickAgent, showSidebar, typeAndSend, uniq, waitChat, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const PILL = 'No project. Attach a project to this session';
const sheet = (page) => page.getByRole('dialog', { name: 'Attach a project' });
const pill = (page) => page.getByRole('button', { name: PILL, exact: true });

async function sendAndSettle(page, variant, words, messagesAtLeast) {
	await typeAndSend(page, `${words} ${fakeAiTag(variant)}`.trim());
	await waitTurnSettled(page, { messagesAtLeast, text: `You said: “${words}` });
}

/** How many provider requests so far carried `needle` (the fake AI's own log). */
async function providerRequestsWith(status, needle) {
	const text = await readFile(status.fake_ai.request_log, 'utf8').catch(() => '');
	return text.trim().split('\n').filter(Boolean)
		.filter((line) => JSON.stringify(JSON.parse(line).body).includes(needle)).length;
}

/** A chat's title, found by a word from its first prompt. */
async function chatTitleByToken(page, token) {
	for (let attempt = 0; attempt < 20; attempt++) {
		const rows = await api(page, 'GET', '/sessions?limit=50');
		const row = rows.find((r) => (r.title || '').includes(token));
		if (row) return { id: row.id, title: row.title };
		await page.waitForTimeout(500);
	}
	throw new Error(`no chat titled with ${token}`);
}

/** The bound chat's short tag, as the page reports it (ui-signature.ts). */
const chatTag = (page) => page.locator('[data-ui-state="chat"]').getAttribute('data-ui-session');

/**
 * Attaching a project is owner only: a preview grant is refused it
 * (app/core/preview_access.py, issue #371). So the sheet's own request is
 * answered by the dev instance's local-owner port, with the body the sheet
 * sent, and the real answer goes back to the page.
 */
async function answerAsOwner(route, status) {
	const port = status.app?.local_port;
	expect(port, 'the dev instance reports its local-owner port');
	const origin = `http://127.0.0.1:${port}`;
	const response = await fetch(origin + new URL(route.request().url()).pathname, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Origin: origin, 'Sec-Fetch-Site': 'same-origin' },
		body: route.request().postData() || '{}'
	});
	await route.fulfill({ status: response.status, contentType: 'application/json', body: await response.text() });
}

/**
 * Route the sheet's attach request. `hold` keeps it until `release()`;
 * `fail` answers with a refusal instead of the owner's real answer.
 * `done` settles once the handler has answered (or rejects with its error,
 * so a broken handler fails the journey), and `delivered` once the page has
 * the whole response.
 */
async function routeAttach(page, status, { hold = false, fail = false } = {}) {
	let reached, release, finish, abort;
	const requested = new Promise((resolve) => { reached = resolve; });
	const released = hold ? new Promise((resolve) => { release = resolve; }) : Promise.resolve();
	const done = new Promise((resolve, reject) => { finish = resolve; abort = reject; });
	done.catch(() => {}); // observed by whoever awaits it
	const delivered = page.waitForEvent('requestfinished', { predicate: (r) => r.url().endsWith('/attach-project'), timeout: 120_000 });
	delivered.catch(() => {});
	await page.route('**/attach-project', async (route) => {
		reached();
		try {
			await released;
			if (fail) {
				await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'journey: attach refused late' }) });
			} else {
				await answerAsOwner(route, status);
			}
			finish();
		} catch (error) {
			abort(error);
		}
	});
	return { requested, release: () => release?.(), done, delivered };
}

/** Within `ms`, or a named failure (never a silent pass). */
function within(promise, ms, what) {
	let timer;
	return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`timed out after ${ms}ms waiting for ${what}`)), ms); })])
		.finally(() => clearTimeout(timer));
}

/** Two animation frames: the page has run what the delivered answer queued. */
const twoFrames = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

async function openSheetAndPick(page) {
	await pill(page).click();
	await sheet(page).waitFor({ timeout: 10_000 });
	const radio = sheet(page).getByRole('radio', { name: PROJECT, exact: true });
	await radio.click();
	await page.waitForFunction((name) => [...document.querySelectorAll('[role="dialog"] [role="radio"]')]
		.some((r) => (r.textContent || '').trim() === name && r.getAttribute('aria-checked') === 'true'), PROJECT, { timeout: 5_000 });
}

/** Two chats with no project, the first one open, the second one just before it in history. */
async function twoChatsWithoutProject(ctx) {
	const { page, variant } = ctx;
	await openNewChat(ctx);
	const first = `first-${uniq()}`;
	await sendAndSettle(page, variant, first, 2);
	const tagA = await chatTag(page);
	// The sidebar's New chat: a fresh load would reopen the chat just made.
	await showSidebar(page);
	await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'New chat', exact: true }).click();
	await waitChat(page, { surface: 'new' });
	await pickAgent(page, page.getByRole('region', { name: 'New chat' }));
	const second = `second-${uniq()}`;
	await sendAndSettle(page, variant, second, 2);
	const tagB = await chatTag(page);
	const a = await chatTitleByToken(page, first);
	const b = await chatTitleByToken(page, second);
	await openFromSidebar(page, a.title);
	await waitChat(page, { session: tagA, loading: 'false' }, { timeout: 30_000 });
	await pill(page).waitFor({ timeout: 15_000 });
	ctx.state = { a: { ...a, tag: tagA }, b: { ...b, tag: tagB } };
}

async function retryBeforeOriginalAnswer({ page, mark, state, status }) {
	await openSheetAndPick(page);
	const url = '**/attach-project';
	// The first route waits until the retry has committed. Both requests go
	// to the real owner, which must then refuse the first as already attached.
	const first = await routeAttach(page, status, { hold: true });
	await mark('hold the first attachment and leave A');
	await sheet(page).getByRole('button', { name: `Attach ${PROJECT}`, exact: true }).click();
	await within(first.requested, 30_000, 'first attach');
	await page.goBack();
	await waitChat(page, {session: state.b.tag, loading: 'false'});
	await mark('return to A before its first attach completes');
	await page.goForward();
	await waitChat(page, {session: state.a.tag, loading: 'false'});
	await openSheetAndPick(page);
	await mark('attach successfully on the second try');
	await page.route(url, route => answerAsOwner(route, status), {times: 1});
	const retry = page.waitForResponse(r => r.url().endsWith('/attach-project'));
	await sheet(page).getByRole('button', {name: `Attach ${PROJECT}`, exact: true}).click();
	expect((await retry).status() === 200, 'retry committed on the owner');
	await sheet(page).waitFor({state:'hidden'});
	const committed = await api(page, 'GET', `/sessions/${state.a.id}`);
	expect(!!committed.project_v2_id, 'retry attached A to a project');
	await mark('release the first request after the retry');
	const old = page.waitForResponse(r => r.url().endsWith('/attach-project'));
	first.release();
	const response = await old;
	expect(response.status() === 409 && /already has a project/i.test((await response.json()).detail), 'owner refused the obsolete attach');
	await within(first.done, 30_000, 'old attach handler');
	await twoFrames(page);
	await waitChat(page, {session: state.a.tag, loading: 'false'});
	expect(!(await page.getByRole('alert').filter({hasText:/already has a project/i}).isVisible()), 'no stale refusal in A');
	expect(!(await pill(page).isVisible()), 'A still shows the attached project');
	expect((await api(page, 'GET', `/sessions/${state.a.id}`)).project_v2_id === committed.project_v2_id, 'A kept the same attached project');
}

/** Press Attach with the answer held, move back to the other chat, then let the answer through. */
function lateAnswerAfterSwitch(fail, returnFirst = false) {
	return async ({ page, mark, state, status }) => {
		await mark('open the sheet and pick a project');
		await openSheetAndPick(page);
		const attach = await routeAttach(page, status, { hold: true, fail });
		await mark('press Attach, then go back to the other chat');
		await sheet(page).getByRole('button', { name: `Attach ${PROJECT}`, exact: true }).click();
		await within(attach.requested, 30_000, 'the attach request');
		await page.goBack();
		await waitChat(page, { session: state.b.tag, loading: 'false' }, { timeout: 30_000 });
		await waitChatOpen(page, { messagesAtLeast: 2, hold: 500 });
		expect(!(await sheet(page).isVisible()), 'the sheet closed when the page moved to the other chat');
		expect((await pill(page).getAttribute('aria-expanded')) === 'false', 'the other chat\'s pill is not expanded');
		if (returnFirst) {
			await mark('go forward to the first chat before its attach answers');
			await page.goForward();
			await waitChat(page, { session: state.a.tag, loading: 'false' });
			attach.release();
			await within(attach.delivered, 60_000, 'A receives the delayed refusal');
			await page.getByRole('alert').filter({ hasText: 'journey: attach refused late' }).waitFor();
			expect(!(await sheet(page).isVisible()), 'the old sheet was not reopened');
			return;
		}
		await mark('the attach answer arrives late');
		attach.release();
		await within(attach.done, 60_000, 'the late attach answer');
		await within(attach.delivered, 60_000, 'the page to receive the late answer');
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await twoFrames(page);
		await waitChat(page, { session: state.b.tag }, { timeout: 5_000 });
		expect(!(await sheet(page).isVisible()), 'the late answer did not reopen the sheet');
		expect((await sheet(page).count()) === 0, 'no sheet is mounted after the late answer');
		await mark('open the sheet in this chat');
		await pill(page).click();
		await sheet(page).waitFor({ timeout: 10_000 });
		const picked = await sheet(page).locator('[role="radio"][aria-checked="true"]').count();
		expect(picked === 0, `nothing is picked in this chat's sheet (found ${picked})`);
		expect(await sheet(page).getByRole('button', { name: 'Attach', exact: true }).isDisabled(), 'Attach waits for a pick');
		expect((await sheet(page).getByRole('alert').count()) === 0, 'no error text carried into this chat\'s sheet');
		await sheet(page).getByRole('button', { name: 'Cancel', exact: true }).click();
		await sheet(page).waitFor({ state: 'hidden', timeout: 10_000 });
		if (fail) {
			const refusal = page.getByRole('alert').filter({ hasText: 'journey: attach refused late' });
			expect(!(await refusal.isVisible()), 'the refusal does not appear on the other chat');
			await openFromSidebar(page, state.a.title);
			await waitChat(page, { session: state.a.tag, loading: 'false' });
			await refusal.waitFor({ timeout: 10_000 });
		} else {
			const row = await api(page, 'GET', `/sessions/${state.a.id}`);
			expect(!!(row.project_id || row.project_v2_id), 'the attach still happened on the first chat');
		}
	};
}

await runJourney({
	name: 'attach-project',
	cases: [
		{
			name: 'rewound-attachment-resends',
			async setup(ctx) {
				await openNewChat(ctx);
				const kept = `kept-${uniq()}`;
				await sendAndSettle(ctx.page, ctx.variant, kept, 2);
				await pill(ctx.page).waitFor({ timeout: 15_000 });
				ctx.state = { kept, words: `attached-${uniq()}`, file: `note-${uniq()}.txt` };
			},
			async act({ page, mark, state, variant, status }) {
				const box = composer(page);
				await mark('attach a file and send');
				await page.locator('input[type="file"]').first().setInputFiles({ name: state.file, mimeType: 'text/plain', buffer: Buffer.from('resend me\n') });
				await box.locator('[data-file-chip]').first().waitFor({ timeout: 20_000 });
				await box.focus();
				await page.keyboard.press('ControlOrMeta+End');
				await box.pressSequentially(` ${state.words} ${fakeAiTag(variant)}`.trimEnd());
				await page.getByRole('button', { name: 'Send', exact: true }).click();
				await waitTurnSettled(page, { messagesAtLeast: 4, text: 'You said:' });
				const sentOnce = await providerRequestsWith(status, state.file);
				expect(sentOnce >= 1, 'the first send reached the AI with the file');

				await mark('attach a project');
				await openSheetAndPick(page);
				const attach = await routeAttach(page, status);
				await sheet(page).getByRole('button', { name: `Attach ${PROJECT}`, exact: true }).click();
				await within(attach.done, 60_000, 'the attach answer');
				await within(attach.delivered, 60_000, 'the page to receive the attach answer');
				await sheet(page).waitFor({ state: 'hidden', timeout: 30_000 });
				await pill(page).waitFor({ state: 'hidden', timeout: 30_000 });
				await page.unrouteAll({ behavior: 'ignoreErrors' });

				await mark('rewind the message with the attachment');
				await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
				await page.keyboard.press('Enter');
				await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
				await waitChat(page, { messages: '2' }, { timeout: 30_000 });
				await box.getByText(state.words).waitFor({ timeout: 15_000 });
				expect((await box.locator('[data-file-chip]').count()) === 1, 'the file came back as a chip');

				await mark('send it again');
				await page.getByRole('button', { name: 'Send', exact: true }).click();
				await waitTurnSettled(page, { messagesAtLeast: 4, text: 'You said:' });
				const refused = page.getByText(/Attachments need|outside (the|this) (chat|session)|not allowed|refused/i).first();
				expect(!(await refused.isVisible().catch(() => false)), 'the resend was not refused');
				for (let i = 0; i < 240 && (await providerRequestsWith(status, state.file)) <= sentOnce; i++) await page.waitForTimeout(250); // polls the log, up to 60 s
				const sentTwice = await providerRequestsWith(status, state.file);
				expect(sentTwice > sentOnce, `the resend reached the AI with the file (${sentOnce} → ${sentTwice})`);
			}
		},
		{ name: 'retry-before-old-refusal', setup: twoChatsWithoutProject, act: retryBeforeOriginalAnswer,
			allow: [{ kind: 'failed_requests', status: 409, url: /\/attach-project$/, reason: 'the owner refuses the original attach after the retry committed' }] },
		{ name: 'late-success-after-switch', setup: twoChatsWithoutProject, act: lateAnswerAfterSwitch(false) },
		{
			name: 'late-error-after-return',
			setup: twoChatsWithoutProject,
			act: lateAnswerAfterSwitch(true, true),
			allow: [{ kind: 'failed_requests', status: 409, url: /\/attach-project$/, reason: 'the held request is refused' }]
		},
		{
			name: 'late-error-after-switch',
			setup: twoChatsWithoutProject,
			act: lateAnswerAfterSwitch(true),
			allow: [{ kind: 'failed_requests', status: 409, url: /\/attach-project$/, reason: 'the case answers the attach with a refusal on purpose' }]
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});

