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
//   long-work   the agent works ~15 s ([script:voice-long]): its progress sentence is said once as a
//               short line before the short result, never after it; on Grok the voice's check_work, called
//               while the work runs, is answered with the running work and starts nothing
//   long-work-quiet  as long-work, but the caller asked to hear only the result: no progress line
//   check-idle  Grok only: with nothing running, the voice answers a new request ("Check whether the
//               deploy finished.") with check_work instead of a handoff; the app takes it as the
//               handoff: one request reaches the agent, a short "on it", the reply said once
//   status-after-small-talk  (#1547) nothing running: answered small talk, then "Is it done yet?" handed
//               off; it is answered from the task state and starts no chat turn (no fake-AI request)
//   status-twice (#1547) two status questions in a row at idle, each handed off: both answered, no chat turn
//   mute-mixed  (#1547) on long work, "just tell me when it's done" then "Keep me posted, skip the
//               updates and reboot the server": progress is back on, the progress line is said
//   mute-work-request (#1547) "Skip the updates and reboot the server" is a work request, not a mute:
//               the progress line is still said
//   attachment-during-call (#1265) a file sent while a call is live goes as fields beside the words
//               and shows as a chip on the message
import { runJourney } from './runner.mjs';
import { readFile } from 'node:fs/promises';
import { anyFrame } from './lib/timeline.mjs';
import { PROJECT, composer, countUserRows, expect, openNewChat, pickAccess, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
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

const LONG_WORK = {
	progress: "I'm reading through the test logs now.",
	result: 'One test is flaky, the upload retry test. Everything else passed. Should I mark it flaky?'
};

/** Long work: a short progress line while the agent works (unless asked not to), then the result. */
function longWork(name, { quiet }) {
	return {
		name,
		throttles: ['fast'],
		async setup(ctx) {
			ctx.token = await chatWithScenario(ctx, name);
		},
		async act({ page, mark, status, log }) {
			const voice = fakeVoiceLog(status);
			const seq = voice.mark();
			await mark('start the call; the caller asks for something that takes a while');
			await startCall(page, { timeout: 45_000 });
			const session = await callSession(voice, seq, name);
			await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text.includes(LONG_WORK.result)), 'the result said aloud', { timeout: 90_000 });
			const grok = voice.since(seq).some((e) => e.session === session && e.dialect === 'grok');
			if (grok && !quiet) await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'tool_output' && e.tool === 'check_work'), 'check_work answered', { timeout: 30_000 });
			await page.waitForTimeout(3000);
			const entries = voice.since(seq);
			const notes = spokenNotes(entries, session);
			const progress = notes.filter((e) => e.text === LONG_WORK.progress);
			const result = notes.findIndex((e) => e.text.includes(LONG_WORK.result));
			expect(notes.filter((e) => e.text.includes(LONG_WORK.result)).length === 1, `the result is said once:\n  ${speechReport(entries)}`);
			if (quiet) {
				expect(progress.length === 0, `no progress line after "just tell me when it's done":\n  ${speechReport(entries)}`);
			} else {
				expect(progress.length === 1, `the progress line is said once:\n  ${speechReport(entries)}`);
				expect(notes.indexOf(progress[0]) < result, 'the progress line comes before the result, never after it');
			}
			if (grok && !quiet) {
				// Looked at while the work runs: the latest progress, never a made-up result.
				const looked = notes.findIndex((e) => e.text.includes('unfinished and not a result'));
				expect(looked > -1 && looked < result, `check_work told the running work before the result:\n  ${speechReport(entries)}`);
				expect(!entries.some((e) => e.kind === 'handoff' && e.seq > notes[looked].seq), 'check_work started no work');
			}
			expect(!entries.some((e) => e.kind === 'unlabelled_user_item'), "no app note reads as the caller's words");
			expect(!entries.some((e) => e.kind === 'error.sent' || e.kind === 'overlap'), 'no provider error or overlapping response');
			log(`${grok ? 'grok' : 'gpt-live'}: ${notes.length} spoken note(s)`);
			await mark('end the call');
			await endCall(page);
		}
	};
}

const checkIdle = {
	name: 'check-idle',
	throttles: ['fast'],
	async setup(ctx) {
		ctx.token = await chatWithScenario(ctx, 'check-idle');
	},
	async act({ page, mark, status, log }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call; the caller asks for a check and the voice calls check_work');
		await startCall(page, { timeout: 45_000 });
		const session = await callSession(voice, seq, 'check-idle');
		const looked = await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'check_work' || e.kind === 'check_work.unsupported'), 'the voice calling check_work');
		if (looked.kind === 'check_work.unsupported') {
			log('gpt-live: no check_work tool, nothing to check');
			await mark('end the call');
			await endCall(page);
			return;
		}
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'check_work.handed_off'), 'check_work taken as the handoff', { timeout: 30_000 });
		await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text.includes('214 tests')), 'the chat reply said aloud', { timeout: 90_000 });
		await page.getByText('All 214 tests pass', { exact: false }).first().waitFor({ timeout: 30_000 });
		await page.waitForTimeout(3000); // a second handoff or reply would land now
		const entries = voice.since(seq);
		const said = entries.filter((e) => e.kind === 'speech');
		expect(said.some((e) => e.trigger === 'ack' && e.text === 'Checking on that now.'), `the voice said it is on it:\n  ${speechReport(entries)}`);
		expectSaidOnce(entries, VOICE_REPLY_SPOKEN, 'the chat reply');
		expect(!entries.some((e) => e.kind === 'handoff'), 'the voice did not hand the request off a second time');
		expect((await countUserRows(page, 'Check whether the deploy finished.')) === 1, "the caller's words reach the chat once");
		expect(!entries.some((e) => e.kind === 'error.sent' || e.kind === 'overlap'), 'no provider error or overlapping response');
		await mark('end the call');
		await endCall(page);
	}
};

/** Every request the fake AI has answered so far (the dev instance's request log). */
async function fakeAiRequests(status) {
	const text = await readFile(status.fake_ai.request_log, 'utf8').catch(() => '');
	return text.split('\n').filter(Boolean).map((line) => { try { return JSON.parse(line); } catch { return {}; } });
}

/** The status the app reads out when nothing runs (voice_status.status_sentence). */
const IDLE_STATUS = 'Nothing is running.';

/**
 * Status questions at idle (#1541, #1547): the caller's newest words are the
 * question, so each handoff is answered from the app's task state and no
 * chat turn starts, after answered small talk or after another status question.
 */
function idleStatus(name, { questions }) {
	return {
		name,
		throttles: ['fast'],
		async setup(ctx) {
			ctx.token = await chatWithScenario(ctx, name);
		},
		async act({ page, mark, status, log }) {
			const voice = fakeVoiceLog(status);
			const seq = voice.mark();
			const requestsBefore = await fakeAiRequests(status);
			await mark('start the call; the caller asks how the work is going');
			await startCall(page, { timeout: 45_000 });
			const session = await callSession(voice, seq, name);
			await voice.waitFor(seq, (es) => spokenNotes(es, session).filter((e) => e.text.includes(IDLE_STATUS)).length >= questions.length,
				`${questions.length} status answer(s) said aloud`, { timeout: 60_000 });
			await page.waitForTimeout(3000); // a chat turn started by the question would show now
			const entries = voice.since(seq);
			expect(entries.filter((e) => e.kind === 'handoff').length === questions.length, `the voice handed off ${questions.length} time(s)`);
			expect(spokenNotes(entries, session).filter((e) => e.text.includes(IDLE_STATUS)).length === questions.length,
				`each status question is answered once from the task state:\n  ${speechReport(entries)}`);
			// The call's own words show in the chat as its transcript; a chat turn
			// is a request to the chat agent, which the fake AI logs.
			const turns = (await fakeAiRequests(status)).slice(requestsBefore.length)
				.filter((r) => questions.some((q) => String(r.prompt || '').includes(q)));
			expect(turns.length === 0, `no status question reached the chat agent: ${turns.map((r) => JSON.stringify(String(r.prompt).slice(-120))).join('; ')}`);
			expect(!entries.some((e) => e.kind === 'error.sent' || e.kind === 'overlap'), 'no provider error or overlapping response');
			log(`${spokenNotes(entries, session).length} spoken note(s), no chat turn`);
			await mark('end the call');
			await endCall(page);
		},
		verify({ recording }) {
			expect(!anyFrame(recording, (s) => s.streaming === 'true'), 'no frame shows a chat turn streaming');
		}
	};
}

/**
 * A sentence that mixes "skip the updates" with a work request is not a mute
 * (#1541, #1547): on long work ([script:voice-long]) the progress line is
 * still said once, before the result.
 */
function mixedMute(name, { sentence }) {
	return {
		name,
		throttles: ['fast'],
		async setup(ctx) {
			ctx.token = await chatWithScenario(ctx, name);
		},
		async act({ page, mark, status, log }) {
			const voice = fakeVoiceLog(status);
			const seq = voice.mark();
			await mark(`start the call; after the handoff the caller says "${sentence}"`);
			await startCall(page, { timeout: 45_000 });
			const session = await callSession(voice, seq, name);
			await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'caller' && e.text === sentence), 'the mixed sentence said by the caller');
			await voice.waitFor(seq, (es) => spokenNotes(es, session).find((e) => e.text.includes(LONG_WORK.result)), 'the result said aloud', { timeout: 90_000 });
			await page.waitForTimeout(3000);
			const entries = voice.since(seq);
			const notes = spokenNotes(entries, session);
			const progress = notes.filter((e) => e.text === LONG_WORK.progress);
			const result = notes.findIndex((e) => e.text.includes(LONG_WORK.result));
			expect(progress.length === 1, `the progress line is still said once after "${sentence}":\n  ${speechReport(entries)}`);
			expect(notes.indexOf(progress[0]) < result, 'the progress line comes before the result');
			expect(notes.filter((e) => e.text.includes(LONG_WORK.result)).length === 1, `the result is said once:\n  ${speechReport(entries)}`);
			expect(!entries.some((e) => e.kind === 'error.sent' || e.kind === 'overlap'), 'no provider error or overlapping response');
			log(`${notes.length} spoken note(s)`);
			await mark('end the call');
			await endCall(page);
		}
	};
}

const attachmentDuringCall = {
	name: 'attachment-during-call',
	// Slow copies only: the fast ones add no signal and cost hosted shard time.
	throttles: ['slow'],
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
		// The preview cookie cannot save execution settings; answer that one save.
		await page.route('**/api/v1/voice/sessions/*/context', route => {
			if (route.request().method() !== 'PATCH') return route.continue();
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
		await waitTurnSettled(page, { messagesAtLeast: 4, text: 'Live conversation context' });
		await mark('end the call');
		await endCall(page);
	}
};

await runJourney({
	name: 'voice-call',
	browser: voiceBrowser,
	cases: [startEnd, relay, waitUpdate, busy, longWork('long-work', { quiet: false }), longWork('long-work-quiet', { quiet: true }), checkIdle,
		idleStatus('status-after-small-talk', { questions: ['Is it done yet?'] }),
		idleStatus('status-twice', { questions: ['Is it done yet?', 'Is anything running right now?'] }),
		mixedMute('mute-mixed', { sentence: 'Keep me posted, skip the updates and reboot the server.' }),
		mixedMute('mute-work-request', { sentence: 'Skip the updates and reboot the server.' }),
		contextAdmissionTimeout, attachmentDuringCall],
	allow: [...DEV_INSTANCE_NOISE]
});
