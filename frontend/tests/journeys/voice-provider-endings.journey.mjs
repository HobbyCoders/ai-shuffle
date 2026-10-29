// Journey: the voice provider ends or breaks a call, and the app recovers as
// designed. Real call from the UI on the dev instance's fake voice provider
// (scripts/devloop/fake_voice.py); see voice-call.journey.mjs for the setup.
//
// Cases:
//   inactivity  the provider ends its session for inactivity. GPT Live: the
//               call ends quietly with a notice (not an error), the composer
//               is back and a new call works. Grok: a clean close is renewed,
//               the call stays up on a resumed session that does not greet again.
//   error       the provider reports an error mid-call: the reason shows, no
//               call controls are left behind, and a retry works
//   renew       the provider ends its session as expired while the chat agent
//               works: the app reconnects (no second greeting) and the reply
//               is said there, once, in full
import { runJourney } from './runner.mjs';
import { PROJECT, expect, openNewChat, pickAccess, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
import {
	VOICE_REPLY_SPOKEN,
	callButton,
	callDock,
	callSession,
	endCall,
	fakeVoiceLog,
	spokenNotes,
	startCall,
	waitCallLive
} from './lib/voice.mjs';
import { voiceBrowser } from './lib/voice.mjs';

async function chatWithScenario(ctx, scenario) {
	await openNewChat(ctx, { project: PROJECT });
	await pickAccess(ctx.page, 'Bypass');
	await typeAndSend(ctx.page, `[voice:${scenario}] voice-${uniq()}`);
	await waitTurnSettled(ctx.page, { messagesAtLeast: 2 });
	await callButton(ctx.page).first().waitFor({ timeout: 30_000 });
}

/**
 * Known bug #816: on a phone the call dock is not the composer's height, so a
 * call the provider ends (no input) moves the transcript as the composer comes
 * back. Only that one shift: a layout shift under 0.1 in total, of the
 * composer slot alone.
 */
const DOCK_SWAP_SHIFT = {
	kind: 'layout_shift',
	message: /^layout shifted 0\.0\d+ in total; biggest 0\.0\d+ at \d+ms \(moved: div\.input-slot in ChatView\)$/,
	reason: 'known bug #816: on a phone the call dock and the composer differ in height, so a provider-ended call moves the transcript'
};

const greetings = (entries, session) => entries.filter((e) => e.kind === 'speech' && e.trigger === 'greeting' && e.session === session);

/** The call is over with nothing of it left on screen: no dock, the call button back. */
async function expectCallGone(page) {
	await callDock(page).waitFor({ state: 'detached', timeout: 20_000 });
	await callButton(page).first().waitFor({ timeout: 20_000 });
}

/** A second call on the same chat goes live (the scenario plays once), then ends. */
async function retryCall(page, voice, mark) {
	const seq = voice.mark();
	await mark('call again');
	await startCall(page);
	await callSession(voice, seq, 'default');
	await page.waitForTimeout(1000);
	await mark('end the second call');
	await endCall(page);
}

const inactivity = {
	name: 'inactivity',
	allow: [DOCK_SWAP_SHIFT],
	async setup(ctx) {
		await chatWithScenario(ctx, 'inactivity');
	},
	async act({ page, mark, status }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call; the provider ends it for inactivity');
		await startCall(page);
		const session = await callSession(voice, seq, 'inactivity');
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'close.sent'), 'the provider ending its session');
		if (status.fake_voice.engine === 'grok') {
			// xAI sends no reason: a clean close is renewed and the call goes on.
			const next = await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'scenario' && e.session > session), 'a renewed provider session');
			expect(next.source === 'resumed', `the renewed session resumes the call (${next.source})`);
			await waitCallLive(page);
			await page.waitForTimeout(2500);
			expect(greetings(voice.since(seq), next.session).length === 0, 'the resumed session does not greet again');
			await mark('end the call');
			await endCall(page);
			return;
		}
		// GPT Live names the reason: the call ends, said as an ending, not a failure.
		await expectCallGone(page);
		await page.getByText('The voice provider ended the call.', { exact: false }).first().waitFor({ timeout: 10_000 });
		expect((await page.getByRole('alert').filter({ hasText: 'ended the call' }).count()) === 0, 'the provider ending the call is not shown as an error');
		await page.waitForTimeout(1000);
		await retryCall(page, voice, mark);
	}
};

const error = {
	name: 'error',
	allow: [DOCK_SWAP_SHIFT],
	async setup(ctx) {
		await chatWithScenario(ctx, 'error');
	},
	async act({ page, mark, status }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call; the provider fails mid-call');
		await startCall(page);
		await callSession(voice, seq, 'error');
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'error.sent'), 'the provider error');
		await expectCallGone(page);
		const reason = status.fake_voice.engine === 'grok' ? 'xAI rejected a voice event' : 'The fake voice provider had an internal error.';
		await page.getByText(reason, { exact: false }).first().waitFor({ timeout: 10_000 });
		await page.waitForTimeout(1000);
		await retryCall(page, voice, mark);
	}
};

const renew = {
	name: 'renew',
	async setup(ctx) {
		await chatWithScenario(ctx, 'renew');
	},
	async act({ page, mark, status, log }) {
		const voice = fakeVoiceLog(status);
		const seq = voice.mark();
		await mark('start the call; the provider session expires while the agent works');
		await startCall(page);
		const first = await callSession(voice, seq, 'renew');
		await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'close.sent'), 'the provider ending its session');
		const next = await voice.waitFor(seq, (es) => es.find((e) => e.kind === 'scenario' && e.session > first), 'the reconnected provider session', { timeout: 30_000 });
		expect(next.source === 'resumed', `the new session resumes the call (${next.source})`);
		const said = await voice.waitFor(seq, (es) => spokenNotes(es).find((e) => e.text.includes('214 tests')), 'the chat reply said after the reconnect', { timeout: 90_000 });
		log(`reply said on provider session ${said.session} (first ${first}, renewed ${next.session})`);
		await waitCallLive(page);
		await page.waitForTimeout(3000);
		const entries = voice.since(seq);
		const replies = spokenNotes(entries).filter((e) => e.text.includes('214 tests'));
		expect(replies.length === 1, `the reply is said once across the reconnect (${replies.length}×)`);
		expect(replies[0].text === VOICE_REPLY_SPOKEN, `the reply is said in full:\n  said: ${replies[0].text}`);
		expect(greetings(entries, next.session).length === 0, 'the reconnected session does not greet again');
		await mark('end the call');
		await endCall(page);
	}
};

await runJourney({
	name: 'voice-provider-endings',
	browser: voiceBrowser,
	throttles: ['fast'],
	cases: [inactivity, error, renew],
	allow: [...DEV_INSTANCE_NOISE]
});
