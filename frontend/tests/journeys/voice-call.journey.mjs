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
import { runJourney } from './runner.mjs';
import { PROJECT, countUserRows, expect, openNewChat, pickAccess, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
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

await runJourney({
	name: 'voice-call',
	browser: voiceBrowser,
	cases: [startEnd, relay, waitUpdate, busy],
	allow: [...DEV_INSTANCE_NOISE]
});
