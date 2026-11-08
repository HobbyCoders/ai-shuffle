// Journey: a voice call on a chat, placed from the UI on the dev instance's
// fake voice provider (scripts/devloop/fake_voice.py). The app's real call
// code runs end to end: the call button, the browser's microphone (Chromium's
// fake device) over the call's audio socket, the server's GPT Live (or Grok)
// transport and the hand-off to the chat agent (the fake AI). Only the
// provider is fake, and its log says what the app asked the voice to say.
//
// The owner's rule for calls: the voice is the chat agent. Every reply the
// agent writes for the caller is said in full, once, never skipped or
// replaced by an acknowledgement, and an update it writes before it waits is
// said too.
//
// Every case runs at desktop and phone size; relay also runs on the slow
// variant, which slows the provider too (late session start, late replies).
//
// Cases:
//   start-end   a call goes live, greets once, ends with the End button; the composer is back
//   relay       the caller asks, the chat agent answers ([script:voice-reply]); the reply is said once, in full
//   wait-update the agent starts a background build and waits ([script:voice-wait]): its update is said before the wait, the result after
//   busy        a call placed while the chat is still answering says that reply once it lands, then relays a new request
//   attachment-during-call (#1265) a file sent while a call is live goes as fields beside the words,
//               shows as a chip on the message, and a rewind brings the chip back to the composer
import { runJourney } from './runner.mjs';
import { PROJECT, api, composer, countUserRows, expect, openNewChat, pickAccess, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import {
	VOICE_REPLY_SPOKEN,
	VOICE_WAIT,
	callButton,
	callSession,
	endCall,
	fakeVoiceLog,
	spokenNotes,
	startCall,
	voiceBrowser
} from './lib/voice.mjs';

/** A project chat with one exchange whose message names the call's scenario. */
async function chatWithScenario(ctx, scenario, { wait = true, extra = '' } = {}) {
	await openNewChat(ctx, { project: PROJECT });
	await pickAccess(ctx.page, 'Bypass');
	const token = `voice-${uniq()}`;
	await typeAndSend(ctx.page, `[voice:${scenario}] ${token} ${extra}`.trim());
	if (wait) await waitTurnSettled(ctx.page, { messagesAtLeast: 2 });
	await callButton(ctx.page).first().waitFor({ timeout: 30_000 });
	return token;
}

/** The variant's slow provider: a late session start and late replies. */
const slowOptions = (variant) => (variant.throttleName === 'slow' ? ' start_delay_ms=1500 reply_delay_ms=800' : '');

/** Every note the app sent for speech after `seq`, with what the fake said for them. */
function speechReport(entries) {
	return entries
		.filter((e) => e.kind === 'speech' || (e.kind === 'note' && e.channel === 'speak'))
		.map((e) => `${e.kind}/${e.trigger || e.channel}: ${(e.text || '').slice(0, 90)}`)
		.join('\n  ');
}

/** The reply was said once, whole, and nothing else was said for it. */
function expectSaidOnce(entries, spoken, what) {
	const said = spokenNotes(entries).filter((e) => e.text.includes(spoken.slice(0, 40)));
	expect(said.length === 1, `${what} is said exactly once (said ${said.length}×):\n  ${speechReport(entries)}`);
	expect(said[0].text === spoken, `${what} is said in full:\n  said: ${said[0].text}\n  want: ${spoken}`);
	return said[0];
}

const startEnd = {
	name: 'start-end',
	throttles: ['fast'],
	async setup(ctx) {
		ctx.token = await chatWithScenario(ctx, 'default');
	},
	async act({ page, mark, status, log }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call');
		await startCall(page);
		const session = await callSession(voice, seq, 'default');
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'speech' && e.trigger === 'greeting'), 'the greeting');
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'audio' && e.heard), 'microphone audio from the browser');
		await page.waitForTimeout(1500);
		await mark('end the call');
		await endCall(page);
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'session.closed' && e.session === session), 'the provider session closing');
		await page.waitForTimeout(1000);
		const entries = voice.since(seq);
		const greetings = entries.filter((e) => e.kind === 'speech' && e.trigger === 'greeting');
		expect(greetings.length === 1, `one greeting (${greetings.length})`);
		expect(spokenNotes(entries).length === 0, `nothing else is said on an idle call:\n  ${speechReport(entries)}`);
		expect(entries.filter((e) => e.kind === 'session.open').length === 1, 'one provider session');
		log(`provider session ${session}: ${entries.length} log entries`);
	}
};

// A stalled execution-settings PATCH must not keep Send waiting forever.
// Release it only after the deadline: the expired attempt must stay dead.
const contextAdmissionTimeout = {
	name: 'context-admission-timeout',
	throttles: ['fast', 'slow'],
	async setup(ctx) {
		ctx.typedFrames = [];
		ctx.page.on('websocket', socket => socket.on('framesent', ({ payload }) => {
			if (typeof payload !== 'string') return;
			try {
				const frame = JSON.parse(payload);
				if (frame.type === 'input_text') ctx.typedFrames.push(frame);
			} catch { /* microphone PCM, not typed text */ }
		}));
		ctx.token = await chatWithScenario(ctx, 'default');
		await startCall(ctx.page);
	},
	async act(ctx) {
		const { page, mark } = ctx;
		let held = null;
		await page.route('**/api/v1/voice/sessions/*/context', route => {
			if (route.request().method() === 'PATCH' && !held) { held = route; return; }
			return route.continue();
		});
		try {
			const draft = `keep-voice-draft-${ctx.token}`;
			await mark('send while execution settings never answer');
			await typeAndSend(page, draft);
			await page.getByText('Could not save voice execution settings. Your message has been kept.', { exact: true }).waitFor({ timeout: 12_000 });
			expect(held !== null, 'the real context PATCH was held');
			expect((await page.getByRole('textbox', { name: 'Message' }).innerText()).trim() === draft, 'the composer restores the refused draft');
			expect(ctx.typedFrames.length === 0, 'no text was sent before saving context');
			await mark('let the expired context save succeed');
			const response = page.waitForResponse(r => r.url() === held.request().url() && r.request().method() === 'PATCH');
			// The preview cookie cannot mutate execution settings. Inject only the
			// late success response; no real settings are changed by this test.
			await held.fulfill({ status: 200, contentType: 'application/json', body: held.request().postData() });
			await response;
			await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
			expect(ctx.typedFrames.length === 0, 'late settings do not send the expired message');
			await mark('end the call with the draft still kept');
			await endCall(page);
			expect((await page.getByRole('textbox', { name: 'Message' }).innerText()).trim() === draft, 'ending the call keeps the draft');
		} finally {
			await page.unroute('**/api/v1/voice/sessions/*/context');
		}
	}
};

const relay = {
	name: 'relay',
	async setup(ctx) {
		ctx.token = await chatWithScenario(ctx, `relay${slowOptions(ctx.variant)}`);
	},
	async act({ page, mark, status, log }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call; the caller asks for a build check');
		await startCall(page, { timeout: 45_000 });
		const session = await callSession(voice, seq, 'relay');
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'handoff'), 'the hand-off to the chat agent');
		const said = await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text.includes('214 tests')), 'the chat reply said aloud', { timeout: 90_000 });
		log(`reply said ${said.text.length} chars from ${said.pieces} append(s)`);
		// The agent's reply is in the chat, and the caller's words show once.
		await page.getByText('All 214 tests pass', { exact: false }).first().waitFor({ timeout: 30_000 });
		await page.waitForTimeout(3000); // anything said late (a second reply) lands now
		const entries = voice.since(seq);
		expectSaidOnce(entries, VOICE_REPLY_SPOKEN, 'the chat reply');
		expect(spokenNotes(entries).length === 1, `only the reply is said for the request:\n  ${speechReport(entries)}`);
		expect(!entries.some((e) => e.kind === 'speech' && e.trigger === 'empty'), 'no empty (double) response');
		expect((await countUserRows(page, 'Can you check the build for me?')) === 1, "the caller's words show once in the chat");
		await mark('end the call');
		await endCall(page);
	}
};

const waitUpdate = {
	name: 'wait-update',
	throttles: ['fast'],
	async setup(ctx) {
		ctx.token = await chatWithScenario(ctx, `wait-update${slowOptions(ctx.variant)}`);
	},
	async act({ page, mark, status }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call; the caller asks for a build');
		await startCall(page, { timeout: 45_000 });
		const session = await callSession(voice, seq, 'wait-update');
		await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text === VOICE_WAIT.update), 'the update written before the wait, said aloud', { timeout: 90_000 });
		await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text.includes(VOICE_WAIT.result)), 'the result after the wait, said aloud', { timeout: 90_000 });
		await page.waitForTimeout(3000);
		const entries = voice.since(seq);
		const notes = spokenNotes(entries);
		expect(notes.filter((e) => e.text === VOICE_WAIT.update).length === 1, `the update is said once:\n  ${speechReport(entries)}`);
		expect(notes.filter((e) => e.text.includes(VOICE_WAIT.result)).length === 1, `the result is said once:\n  ${speechReport(entries)}`);
		expect(notes.findIndex((e) => e.text === VOICE_WAIT.update) < notes.findIndex((e) => e.text.includes(VOICE_WAIT.result)), 'the update comes before the result');
		await mark('end the call');
		await endCall(page);
	}
};

const busy = {
	name: 'busy',
	throttles: ['fast'],
	async setup(ctx) {
		// The chat is still answering ([script:slow]) when the call starts.
		ctx.token = await chatWithScenario(ctx, 'busy', { wait: false, extra: '[script:slow tps=8]' });
		await ctx.page.getByText('This reply streams slowly', { exact: false }).first().waitFor({ timeout: 30_000 });
	},
	async act({ page, mark, status }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call while the chat answers');
		await startCall(page, { timeout: 45_000 });
		const session = await callSession(voice, seq, 'busy');
		await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text.includes('This reply streams slowly')), 'the running reply said once it lands', { timeout: 90_000 });
		await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text.includes('214 tests')), 'the new request relayed', { timeout: 90_000 });
		await page.waitForTimeout(3000);
		const entries = voice.since(seq);
		const notes = spokenNotes(entries);
		expect(notes.filter((e) => e.text.includes('This reply streams slowly')).length === 1, `the running reply is said once:\n  ${speechReport(entries)}`);
		expectSaidOnce(entries, VOICE_REPLY_SPOKEN, 'the new reply');
		await mark('end the call');
		await endCall(page);
	}
};

const attachmentDuringCall = {
	name: 'attachment-during-call',
	async setup(ctx) {
		ctx.typedFrames = [];
		ctx.page.on('websocket', socket => socket.on('framesent', ({ payload }) => {
			if (typeof payload !== 'string') return;
			try {
				const frame = JSON.parse(payload);
				if (frame.type === 'input_text') ctx.typedFrames.push(frame);
			} catch { /* microphone PCM, not typed text */ }
		}));
		ctx.token = await chatWithScenario(ctx, 'default');
		await startCall(ctx.page);
		ctx.state = { file: `note-${uniq()}.txt`, words: `attached-${uniq()}` };
	},
	async act(ctx) {
		const { page, mark, state, log } = ctx;
		const box = composer(page);
		let sessionId = null;
		// The preview cookie cannot save execution settings; answer that one save.
		await page.route('**/api/v1/voice/sessions/*/context', route => {
			if (route.request().method() !== 'PATCH') return route.continue();
			sessionId = route.request().url().match(/voice\/sessions\/([^/]+)\/context/)[1];
			return route.fulfill({ status: 200, contentType: 'application/json', body: route.request().postData() });
		});
		await mark('attach a file and send it into the call');
		await page.locator('input[type="file"]').setInputFiles({ name: state.file, mimeType: 'text/plain', buffer: Buffer.from('during the call\n') });
		await box.locator('[data-file-chip]').first().waitFor({ timeout: 20_000 });
		await box.focus();
		await page.keyboard.press('ControlOrMeta+End');
		await box.pressSequentially(` ${state.words}`);
		await page.getByRole('button', { name: 'Send', exact: true }).click();
		await page.locator('.attachment-chip').filter({ hasText: state.file }).first().waitFor({ timeout: 30_000 });
		expect(ctx.typedFrames.length === 1, `one typed frame went up (saw ${ctx.typedFrames.length})`);
		const paths = ctx.typedFrames[0].upload_paths || [];
		expect(paths.length === 1 && paths[0].endsWith(state.file), `the frame names the file as a field: ${JSON.stringify(paths)}`);
		// Wait for the chat agent's answer: its transcript is what the rewind finds.
		await waitTurnSettled(page, { messagesAtLeast: 4, text: 'Live conversation context' });
		await mark('end the call and rewind the message');
		await endCall(page);
		// The chat agent's transcript is written just after its reply shows, and
		// a rewind reads that transcript: wait for this message to be in it.
		const waitedFrom = Date.now();
		let inTranscript = false;
		for (let attempt = 0; attempt < 40 && !inTranscript; attempt++) {
			if (attempt) await page.waitForTimeout(250);
			const { checkpoints = [] } = await api(page, 'GET', `/commands/rewind/checkpoints/${sessionId}`);
			inTranscript = checkpoints.some((cp) => (cp.full_message || '').includes(state.words));
		}
		log(`the transcript held the message after ${Date.now() - waitedFrom}ms`);
		expect(inTranscript, 'the chat agent transcript holds the typed message');
		await page.getByTitle('Rewind to before this message', { exact: true }).last().focus();
		await page.keyboard.press('Enter');
		await page.getByRole('dialog').getByRole('button', { name: 'Rewind', exact: true }).click();
		await box.getByText(state.words).waitFor({ timeout: 20_000 });
		const chips = await box.locator('[data-file-chip]').count();
		const text = await box.innerText();
		expect(chips === 1, `the attachment came back as one chip (found ${chips})`);
		expect(!/\/\S*uploads\//.test(text), `no raw upload path in the composer: ${JSON.stringify(text)}`);
	}
};

await runJourney({
	name: 'voice-call',
	browser: voiceBrowser,
	cases: [startEnd, relay, waitUpdate, busy, contextAdmissionTimeout, attachmentDuringCall],
	allow: [...DEV_INSTANCE_NOISE]
});
