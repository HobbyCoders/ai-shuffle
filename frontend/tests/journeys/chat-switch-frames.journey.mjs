// Journey: what every painted frame shows while switching chats.
//
// Cases:
//   no-previous-rows-after-select   long ↔ short chat (both opened once, so cached), round
//                                   trips: from the first frame the new chat is selected
//                                   until 2 s later, no frame shows the chat that was left
//                                   (its session tag or any of its rows) (#1352)
//   no-previous-rows-with-attachment the same with a file pending in the long chat's composer
//                                   when it is left, as #1352 was first captured
//   first-open-no-previous-rows     the same judgement for opens the tab has not loaded yet:
//                                   loaded on the long chat, then the short chat and the
//                                   other-project chat for the first time, then back
//   first-open-with-attachment      that, with a file pending in the long chat's composer
//   send-then-switch-straight-back  send in a chat, leave for another at once and come
//                                   straight back: no frame of that chat shows fewer
//                                   messages than it did before leaving (#1142)
//
// JOURNEY_SWITCHES sets the round trips per case (default 3; measurement runs use 10).
import { fakeAiTag, runJourney } from './runner.mjs';
import {
	SEEDED,
	api,
	seededChatId,
	chatIdByToken,
	composer,
	expect,
	gotoApp,
	openFromSidebar,
	openNewChat,
	showSidebar,
	sidebarRow,
	typeAndSend,
	uniq,
	waitChat,
	waitChatOpen,
	waitTurnSettled
} from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import { oldRowsUnderNewTitle, previousChatFrames, shortFrames, transcriptNow, watchSwitchFrames } from './lib/switch-frames.mjs';

const ROUND_TRIPS = Math.max(1, Number(process.env.JOURNEY_SWITCHES || 3));
const WATCH_MS = 2000;

/** Wait until the surface names a chat other than `tag`. */
const waitLeft = (page, tag) =>
	page.waitForFunction((tag) => {
		const el = document.querySelector('[data-ui-state="chat"]');
		const now = el?.getAttribute('data-ui-session');
		return !!now && now !== 'none' && now !== tag;
	}, tag, { timeout: 60_000, polling: 'raf' });

/** Both seeded chats opened once in-page, so every recorded switch is a cached open. */
async function warmBoth(page, status) {
	await gotoApp(page, status);
	await waitChat(page, { surface: 'new' });
	await openFromSidebar(page, SEEDED.no_project);
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 800 });
	await openFromSidebar(page, SEEDED.long);
	await waitChatOpen(page, { messagesAtLeast: 50, hold: 800 });
}

async function attachPending(page) {
	const box = composer(page);
	if ((await box.locator('[data-file-chip]').count()) > 0) return;
	await page.locator('input[type="file"]').first().setInputFiles({ name: `pending-${uniq()}.txt`, mimeType: 'text/plain', buffer: Buffer.from('pending attachment\n') });
	await box.locator('[data-file-chip]').first().waitFor({ timeout: 20_000 });
}

/** One recorded switch to `title`; returns the frames that showed the chat left behind. */
async function switchTo(page, title, messagesAtLeast) {
	const left = await transcriptNow(page);
	const stop = await watchSwitchFrames(page);
	const clicked = Date.now();
	await openFromSidebar(page, title);
	await waitLeft(page, left.session);
	await waitChatOpen(page, { messagesAtLeast, hold: 0 });
	await page.waitForTimeout(Math.max(0, WATCH_MS - (Date.now() - clicked)));
	const frames = await stop();
	// Compact trace for the report: t:session:messages:rows per distinct frame.
	const trace = frames.map((f) => `${f.t}:${f.session}:${f.messages}:${f.rows.length}:${f.active ? f.active.slice(0, 6) : '-'}`).join(' ');
	// Informational: a first open holds the left transcript until the new one paints.
	const underNew = oldRowsUnderNewTitle(frames, left, title);
	const heldMs = underNew.length ? (frames[frames.indexOf(underNew.at(-1)) + 1]?.t ?? frames.at(-1).t) - underNew[0].t : 0;
	return { left, frames, trace, heldMs, ...previousChatFrames(frames, left) };
}

function switchingCase(name, { attachment }) {
	return {
		name,
		async setup({ page, status }) {
			await warmBoth(page, status);
		},
		async act({ page, mark, log }) {
			const flashes = [];
			const targets = [
				[SEEDED.no_project, 2],
				[SEEDED.long, 50]
			];
			for (let trip = 0; trip < ROUND_TRIPS; trip++) {
				for (const [title, atLeast] of targets) {
					// The long chat is the one left with a file pending (it has a project to upload to).
					if (attachment && title === SEEDED.no_project) await attachPending(page);
					await mark(`switch ${trip + 1} to ${title}`);
					const result = await switchTo(page, title, atLeast);
					log(`to ${title}: selected at ${result.selectedAt} ms, ${result.frames.length} distinct frames, ${result.bad.length} showing the chat left, left rows under the new title for ${result.heldMs} ms [${result.trace}]`);
					if (result.bad.length) flashes.push({ trip, title, selectedAt: result.selectedAt, bad: result.bad.slice(0, 4) });
				}
			}
			log(`switches: ${ROUND_TRIPS * targets.length}, with a frame of the chat left: ${flashes.length}`);
			expect(flashes.length === 0, `no frame shows the previous chat after the new one is selected (${JSON.stringify(flashes)})`);
		}
	};
}

/** Loaded on the long chat; every other chat is opened for the first time in `act`. */
function firstOpenCase(name, { attachment }) {
	return {
		name,
		async setup({ page, status }) {
			await gotoApp(page, status, `/?session=${seededChatId(status, 'long')}`);
			await waitChatOpen(page, { messagesAtLeast: 50, hold: 800 });
			if (attachment) await attachPending(page);
		},
		async act({ page, mark, log }) {
			const flashes = [];
			const targets = [
				[SEEDED.no_project, 2],
				[SEEDED.other_project, 2],
				[SEEDED.long, 50]
			];
			for (const [title, atLeast] of targets) {
				await mark(`open ${title}`);
				const result = await switchTo(page, title, atLeast);
				log(`to ${title}: selected at ${result.selectedAt} ms, ${result.frames.length} distinct frames, ${result.bad.length} showing the chat left, left rows under the new title for ${result.heldMs} ms [${result.trace}]`);
				if (result.bad.length) flashes.push({ title, selectedAt: result.selectedAt, bad: result.bad.slice(0, 4) });
			}
			log(`switches: ${targets.length}, with a frame of the chat left: ${flashes.length}`);
			expect(flashes.length === 0, `no frame shows the previous chat after the new one is selected (${JSON.stringify(flashes)})`);
		}
	};
}

await runJourney({
	name: 'chat-switch-frames',
	cases: [
		switchingCase('no-previous-rows-after-select', { attachment: false }),
		switchingCase('no-previous-rows-with-attachment', { attachment: true }),
		firstOpenCase('first-open-no-previous-rows', { attachment: false }),
		firstOpenCase('first-open-with-attachment', { attachment: true }),
		{
			name: 'send-then-switch-straight-back',
			async setup(ctx) {
				const { page, variant } = ctx;
				await openNewChat(ctx);
				ctx.token = uniq();
				await typeAndSend(page, `switch back ${ctx.token} ${fakeAiTag(variant)}`);
				await waitTurnSettled(page, { messagesAtLeast: 2 });
				const id = await chatIdByToken(page, ctx.token);
				ctx.title = (await api(page, 'GET', '/sessions?limit=50')).find((r) => r.id === id).title;
				// Open the other chat once and come back, so both opens below are cached.
				await openFromSidebar(page, SEEDED.no_project);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 800 });
				await openFromSidebar(page, ctx.title);
				await waitChatOpen(page, { messagesAtLeast: 2, hold: 800 });
			},
			async act({ page, mark, log, variant, title }) {
				const short = [];
				for (let trip = 0; trip < ROUND_TRIPS; trip++) {
					const before = await transcriptNow(page);
					await mark(`send ${trip + 1}`);
					await typeAndSend(page, `again ${trip} ${fakeAiTag(variant)}`);
					// The local echo is on screen: that is the count the chat must keep.
					await waitChat(page, { messagesAtLeast: before.messages + 1 });
					const shown = (await transcriptNow(page)).messages;
					const stop = await watchSwitchFrames(page);
					await mark(`leave at once ${trip + 1}`);
					const sessions = await showSidebar(page);
					await sidebarRow(sessions, SEEDED.no_project).click();
					await waitLeft(page, before.session);
					await mark(`come straight back ${trip + 1}`);
					await openFromSidebar(page, title);
					await waitChat(page, { session: before.session });
					await page.waitForTimeout(WATCH_MS);
					const frames = await stop();
					const bad = shortFrames(frames, before.session, shown);
					log(`trip ${trip + 1}: ${shown} shown before leaving, ${frames.length} distinct frames, ${bad.length} short`);
					if (bad.length) short.push({ trip, shown, bad: bad.slice(0, 4).map((f) => ({ t: f.t, messages: f.messages })) });
					// Counted from before the send: a fast reply can already be in `shown`.
					await waitTurnSettled(page, { messagesAtLeast: before.messages + 2 });
				}
				expect(short.length === 0, `the chat switched back to never shows a message fewer (${JSON.stringify(short)})`);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
