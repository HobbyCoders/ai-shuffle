/**
 * Notification Sound Service
 *
 * Plays a short, synthesized chime when a session needs attention:
 *  - 'complete'              → an AI session finished
 *  - 'question'/'permission' → Claude is waiting for the user (question / permission)
 *
 * Design goals
 * ------------
 * 1. Audible regardless of focus/visibility. Once the AudioContext is unlocked,
 *    Web Audio keeps rendering even when the tab is hidden or in the background
 *    (notes are scheduled against `ctx.currentTime`, which is not throttled the
 *    way `setTimeout` is). So the alert plays whether or not the user is looking
 *    at the screen — which is the whole point.
 * 2. No binary assets. Tones are synthesized with oscillators, so this works
 *    offline and needs no audio files in the build.
 * 3. Respects browser autoplay rules. Audio can only start after a user gesture,
 *    so we lazily unlock the context on the first pointer/key/touch interaction.
 *
 * SSR-safe: every entry point guards `typeof window`.
 */

type SoundKind = 'complete' | 'question' | 'permission';

interface Note {
	/** Frequency in Hz */
	freq: number;
	/** Start offset from the beginning of the sequence, in seconds */
	at: number;
	/** Duration in seconds */
	dur: number;
	/** Per-note gain multiplier (0–1), relative to the master volume */
	gain?: number;
}

// A gentle rising major arpeggio (C5 → E5 → G5) — reads as "done / success".
const COMPLETE_NOTES: Note[] = [
	{ freq: 523.25, at: 0.0, dur: 0.18 },
	{ freq: 659.25, at: 0.11, dur: 0.18 },
	{ freq: 783.99, at: 0.22, dur: 0.3 },
];

// A bright rising two-tone (A5 → C#6) — reads as "attention needed".
const QUESTION_NOTES: Note[] = [
	{ freq: 880.0, at: 0.0, dur: 0.16 },
	{ freq: 1108.73, at: 0.17, dur: 0.28 },
];

const STORAGE_KEY = 'ai-shuffle-notification-sound';
const DEFAULT_VOLUME = 0.4;

class NotificationSound {
	private ctx: AudioContext | null = null;
	private enabled = true;
	private volume = DEFAULT_VOLUME;
	private unlocked = false;
	private removeGestureListeners: (() => void) | null = null;

	constructor() {
		if (typeof window === 'undefined') return;
		this.enabled = this.loadEnabled();
		this.attachGestureListeners();
	}

	// ── Preference (enable/disable) ───────────────────────────────────────────

	private loadEnabled(): boolean {
		try {
			const v = localStorage.getItem(STORAGE_KEY);
			return v === null ? true : v === 'true';
		} catch {
			return true;
		}
	}

	isEnabled(): boolean {
		return this.enabled;
	}

	setEnabled(value: boolean): void {
		this.enabled = value;
		try {
			localStorage.setItem(STORAGE_KEY, String(value));
		} catch {
			// localStorage unavailable (private mode, etc.) — keep the in-memory value
		}
		// Turning it on is a good moment to unlock audio (we're inside a click).
		if (value) this.unlock();
	}

	setVolume(value: number): void {
		this.volume = Math.max(0, Math.min(1, value));
	}

	// ── Autoplay unlock ───────────────────────────────────────────────────────

	private attachGestureListeners(): void {
		if (this.removeGestureListeners) return;
		const handler = () => this.unlock();
		const opts = { passive: true } as AddEventListenerOptions;
		window.addEventListener('pointerdown', handler, opts);
		window.addEventListener('keydown', handler, opts);
		window.addEventListener('touchstart', handler, opts);
		this.removeGestureListeners = () => {
			window.removeEventListener('pointerdown', handler);
			window.removeEventListener('keydown', handler);
			window.removeEventListener('touchstart', handler);
			this.removeGestureListeners = null;
		};
	}

	/**
	 * Create/resume the AudioContext. Safe to call repeatedly and from a user
	 * gesture (the listeners above call this). Once running, gesture listeners
	 * are detached.
	 */
	unlock(): void {
		const ctx = this.ensureContext();
		if (!ctx) return;
		if (ctx.state === 'suspended') {
			ctx.resume().then(() => this.markUnlocked()).catch(() => {});
		} else {
			this.markUnlocked();
		}
	}

	private markUnlocked(): void {
		this.unlocked = true;
		this.removeGestureListeners?.();
	}

	private ensureContext(): AudioContext | null {
		if (typeof window === 'undefined') return null;
		if (this.ctx) return this.ctx;
		const Ctor =
			window.AudioContext ||
			(window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
		if (!Ctor) return null;
		try {
			this.ctx = new Ctor();
		} catch {
			return null;
		}
		return this.ctx;
	}

	// ── Playback ──────────────────────────────────────────────────────────────

	play(kind: SoundKind): void {
		if (!this.enabled) return;
		const ctx = this.ensureContext();
		if (!ctx) return;

		// Best-effort resume: the context may have been suspended (autoplay policy,
		// or the OS pausing audio while the tab was backgrounded). If it was ever
		// unlocked this resolves immediately; otherwise the sound is simply skipped.
		if (ctx.state === 'suspended') {
			ctx.resume().catch(() => {});
		}

		const notes = kind === 'complete' ? COMPLETE_NOTES : QUESTION_NOTES;
		this.playSequence(ctx, notes);
	}

	playComplete(): void {
		this.play('complete');
	}

	playQuestion(): void {
		this.play('question');
	}

	private playSequence(ctx: AudioContext, notes: Note[]): void {
		const start = ctx.currentTime + 0.02;
		for (const note of notes) {
			this.playNote(ctx, note.freq, start + note.at, note.dur, note.gain ?? 1);
		}
	}

	private playNote(
		ctx: AudioContext,
		freq: number,
		at: number,
		dur: number,
		gainScale: number
	): void {
		const osc = ctx.createOscillator();
		const gain = ctx.createGain();
		osc.type = 'triangle';
		osc.frequency.value = freq;

		const peak = Math.max(0.0002, this.volume * gainScale);
		// Quick attack, exponential decay — a soft "ding". (exponentialRamp can't
		// reach 0, so we floor at 0.0001 and stop the oscillator afterward.)
		gain.gain.setValueAtTime(0.0001, at);
		gain.gain.exponentialRampToValueAtTime(peak, at + 0.015);
		gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);

		osc.connect(gain);
		gain.connect(ctx.destination);
		osc.start(at);
		osc.stop(at + dur + 0.05);
	}
}

// ── Singleton export ──────────────────────────────────────────────────────────

export const notificationSound = new NotificationSound();
