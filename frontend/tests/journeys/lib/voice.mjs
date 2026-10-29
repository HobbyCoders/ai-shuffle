// Voice-call steps for UI journeys: a real call from the UI, on the dev
// instance's fake voice provider (scripts/devloop/fake_voice.py).
//
// Chromium's fake media gives the call a microphone: a generated tone (a WAV
// in the temp directory, never in the repo) that reaches the app as real PCM
// over the call's audio socket. The fake provider plays a scenario picked by
// `[voice:NAME]` in the chat before the call, and logs what the app asked it
// to say (`speech`, `note`) and what it heard. Journeys judge the UI with the
// flight recorder and the call by that log.
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect } from './app.mjs';

/** The spoken form (app.core.voice_progress.speech_text) of the fake AI's [script:voice-reply]. */
export const VOICE_REPLY_SPOKEN =
	'I checked the build on main. All 214 tests pass and the type check is clean. ' +
	'Two things need your attention: the linter flags an unused import in the app module, and the release notes still mention the old install command. ' +
	'I left both alone for now. Should I fix the lint warning and update the release notes?';
/** [script:voice-wait]: the update written before the wait, and the result after it. */
export const VOICE_WAIT = {
	update: "The build is running in the background now. I'll wait for it and tell you the result as soon as it finishes.",
	result: 'The build finished and passed. Nothing else needs doing.'
};

/**
 * The caller's microphone: 0.6 s of a 440 Hz tone at about -12 dBFS (well
 * above the call's microphone gate), then 3.4 s of silence, looped by
 * Chromium. A caller who pauses: constant sound with no words would trip the
 * app's "provider stopped hearing" check (PROVIDER_DEAF_VOICED_S) once the
 * scripted caller has spoken.
 */
function toneWav() {
	const rate = 48000;
	const samples = rate * 4;
	const voiced = Math.round(rate * 0.6);
	const data = Buffer.alloc(samples * 2);
	for (let i = 0; i < voiced; i++) data.writeInt16LE(Math.round(8000 * Math.sin((2 * Math.PI * 440 * i) / rate)), i * 2);
	const header = Buffer.alloc(44);
	header.write('RIFF', 0);
	header.writeUInt32LE(36 + data.length, 4);
	header.write('WAVEfmt ', 8);
	header.writeUInt32LE(16, 16);
	header.writeUInt16LE(1, 20); // PCM
	header.writeUInt16LE(1, 22); // mono
	header.writeUInt32LE(rate, 24);
	header.writeUInt32LE(rate * 2, 28);
	header.writeUInt16LE(2, 32);
	header.writeUInt16LE(16, 34);
	header.write('data', 36);
	header.writeUInt32LE(data.length, 40);
	const directory = join(process.env.JOURNEY_OUT || join(tmpdir(), 'journeys'), '.media');
	mkdirSync(directory, { recursive: true });
	const path = join(directory, 'caller-tone-v2.wav');
	if (!existsSync(path)) writeFileSync(path, Buffer.concat([header, data]));
	return path;
}

/**
 * runJourney's `browser` hook: a fake microphone playing the tone, no
 * permission prompt, audio that plays without a gesture. A dev instance on a
 * LAN address (`--browser-host`) is not a secure context, where pages get no
 * microphone, so its origin is treated as one.
 */
export function voiceBrowser(status) {
	if (!status.fake_voice?.log) throw new Error('this dev instance has no fake voice provider (status.fake_voice); restart it with this checkout');
	const origin = new URL(status.urls.ui).origin;
	return {
		args: [
			'--use-fake-ui-for-media-stream',
			'--use-fake-device-for-media-stream',
			`--use-file-for-fake-audio-capture=${toneWav()}`,
			'--autoplay-policy=no-user-gesture-required',
			`--unsafely-treat-insecure-origin-as-secure=${origin}`
		],
		context: { permissions: ['microphone'] }
	};
}

/** The fake voice provider's log (JSONL), read from the file the dev instance names. */
export function fakeVoiceLog(status) {
	const path = status.fake_voice.log;
	const entries = () => {
		if (!existsSync(path)) return [];
		return readFileSync(path, 'utf8')
			.split('\n')
			.filter(Boolean)
			.map((line) => JSON.parse(line));
	};
	return {
		/** The latest seq: entries after it belong to what follows. */
		mark: () => entries().reduce((max, e) => Math.max(max, e.seq), 0),
		since: (seq) => entries().filter((e) => e.seq > seq),
		/** Wait until `test(entriesSince)` returns a truthy value; return it. */
		async waitFor(seq, test, what, { timeout = 60_000 } = {}) {
			const deadline = Date.now() + timeout;
			for (;;) {
				const found = test(entries().filter((e) => e.seq > seq));
				if (found) return found;
				if (Date.now() > deadline) {
					const tail = entries()
						.filter((e) => e.seq > seq)
						.slice(-12)
						.map((e) => `${e.seq} ${e.kind} ${JSON.stringify({ ...e, seq: undefined, ts: undefined, kind: undefined }).slice(0, 160)}`);
					throw new Error(`fake voice: ${what} never happened in ${timeout}ms. Last log entries:\n  ${tail.join('\n  ')}`);
				}
				await new Promise((resolve) => setTimeout(resolve, 150));
			}
		}
	};
}

/** Speech the fake said for notes (trigger "note"), optionally only on one session. */
export const spokenNotes = (entries, session = null) =>
	entries.filter((e) => e.kind === 'speech' && e.trigger === 'note' && (session === null || e.session === session));

/** The call dock (VoiceCallPanel) and its status line. */
export const callDock = (page) => page.getByTestId('voice-call-dock');
export const callStatus = (page) => callDock(page).getByRole('status');
export const callButton = (page) => page.getByRole('button', { name: 'Start a call in this conversation' });
export const endCallButton = (page) => page.getByRole('button', { name: 'Stop voice agent' });

/** Press the composer's call button and wait until the call is live. */
export async function startCall(page, { timeout = 30_000 } = {}) {
	await callButton(page).first().click();
	await waitCallLive(page, { timeout });
}

export async function waitCallLive(page, { timeout = 30_000 } = {}) {
	try {
		await callStatus(page).filter({ hasText: /^(Listening|Speaking…|Hearing you…)$/ }).waitFor({ timeout });
	} catch (error) {
		const now = await callStatus(page).innerText().catch(() => '(no call dock)');
		throw new Error(`the call never went live; its status reads "${now}" (${error.message.split('\n')[0]})`);
	}
}

/** End the call with the dock's button; the dock goes and the composer is back. */
export async function endCall(page) {
	await endCallButton(page).click();
	await callDock(page).waitFor({ state: 'detached', timeout: 15_000 });
	await page.getByRole('textbox', { name: 'Message' }).waitFor({ timeout: 15_000 });
}

/**
 * The one session the call opened at the fake (its `session.open` after `seq`),
 * and its scenario.
 */
export async function callSession(log, seq, scenario) {
	const chosen = await log.waitFor(seq, (es) => es.find((e) => e.kind === 'scenario'), 'a provider session that chose its scenario');
	expect(chosen.name === scenario, `the call plays the ${scenario} scenario (it chose ${chosen.name} from ${chosen.source})`);
	return chosen.session;
}
