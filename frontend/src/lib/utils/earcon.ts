/**
 * Earcons — very short synthesised tones that tell the user what the microphone
 * is doing without them having to look at the screen.
 *
 * Why synthesised rather than audio files: these need to be exact, tiny, and
 * available with no network fetch on the critical path. The "ready" cue in
 * particular is a promise that capture is live *right now* — if it had to wait
 * on a download it would be a lie the first time it played.
 *
 * iOS note: an AudioContext created outside a user gesture starts suspended and
 * stays that way. `primeEarcons()` exists to be called synchronously from the
 * click/keydown that starts recording, so the context is already running by the
 * time the cue fires several awaits later.
 *
 * Feedback into the recording is handled by the microphone's own echo
 * cancellation, which every capture path here enables. The cues are also kept
 * short and quiet enough that server VAD does not mistake one for speech.
 */

const PEAK_GAIN = 0.09;

/** Attack long enough to avoid a click, short enough to feel instant. */
const ATTACK_SECONDS = 0.008;

type Tone = { freq: number; start: number; duration: number };

/**
 * The cue vocabulary. Rising = go, falling = done, low buzz = problem — the
 * conventional mapping, so it needs no learning.
 */
const CUES: Record<string, Tone[]> = {
	// Capture is live. Two quick ascending notes; the second is the "go".
	ready: [
		{ freq: 660, start: 0, duration: 0.06 },
		{ freq: 990, start: 0.055, duration: 0.085 }
	],
	// Recording stopped, audio is being processed.
	stop: [
		{ freq: 780, start: 0, duration: 0.055 },
		{ freq: 520, start: 0.05, duration: 0.08 }
	],
	// Something failed, or nothing was heard.
	error: [
		{ freq: 320, start: 0, duration: 0.09 },
		{ freq: 240, start: 0.085, duration: 0.13 }
	]
};

export type EarconName = keyof typeof CUES;

let context: AudioContext | null = null;
let enabled = true;

function ctor(): typeof AudioContext | null {
	if (typeof window === 'undefined') return null;
	return (
		window.AudioContext ||
		(window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext ||
		null
	);
}

/**
 * Turn cues on or off. Driven by the user preference; when off, `playEarcon`
 * becomes a no-op and no AudioContext is ever created.
 */
export function setEarconsEnabled(value: boolean): void {
	enabled = value;
	if (!value && context) {
		context.close().catch(() => {});
		context = null;
	}
}

/**
 * Create and resume the shared AudioContext. Call this **synchronously** from
 * the user gesture that begins recording — on iOS that gesture is the only
 * moment the context is allowed to start.
 */
export function primeEarcons(): void {
	if (!enabled) return;
	const Ctor = ctor();
	if (!Ctor) return;
	try {
		if (!context) context = new Ctor();
		if (context.state === 'suspended') void context.resume();
	} catch {
		context = null;
	}
}

/**
 * Play one cue. Never throws and never blocks — audio is a nicety, and a
 * failure here must not disturb the recording it is describing.
 */
export function playEarcon(name: EarconName): void {
	if (!enabled) return;
	const tones = CUES[name];
	if (!tones) return;

	primeEarcons();
	if (!context) return;

	try {
		const now = context.currentTime;
		for (const tone of tones) {
			const osc = context.createOscillator();
			const gain = context.createGain();

			osc.type = 'sine';
			osc.frequency.setValueAtTime(tone.freq, now + tone.start);

			// Ramp both edges. A gain that jumps to or from zero produces an
			// audible click that reads as a glitch rather than a signal.
			const begin = now + tone.start;
			const end = begin + tone.duration;
			gain.gain.setValueAtTime(0.0001, begin);
			gain.gain.exponentialRampToValueAtTime(PEAK_GAIN, begin + ATTACK_SECONDS);
			gain.gain.exponentialRampToValueAtTime(0.0001, end);

			osc.connect(gain);
			gain.connect(context.destination);
			osc.start(begin);
			osc.stop(end + 0.01);
		}
	} catch {
		/* An unavailable or throttled audio device is not worth reporting. */
	}
}
