/**
 * Tests for notificationSound.ts — enable/persist, autoplay unlock, and that
 * the chime synthesizes the expected number of notes regardless of context state.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const STORAGE_KEY = 'ai-shuffle-notification-sound';

/** Install a fake Web Audio implementation and return inspectable handles. */
function installFakeAudio(state: 'running' | 'suspended' = 'running') {
	const oscillators: unknown[] = [];
	let context: FakeCtx;
	const resume = vi.fn(async () => { context.state = 'running'; });

	class FakeOsc {
		type = 'sine';
		frequency = { value: 0 };
		connect = vi.fn();
		start = vi.fn();
		stop = vi.fn();
		constructor() {
			oscillators.push(this);
		}
	}
	class FakeGain {
		gain = {
			setValueAtTime: vi.fn(),
			exponentialRampToValueAtTime: vi.fn(),
		};
		connect = vi.fn();
	}
	class FakeCtx {
		constructor() { context = this; }
		state = state;
		currentTime = 0;
		destination = {};
		createOscillator() {
			return new FakeOsc();
		}
		createGain() {
			return new FakeGain();
		}
		resume = resume;
	}

	(window as unknown as { AudioContext: unknown }).AudioContext = FakeCtx;
	(window as unknown as { webkitAudioContext: unknown }).webkitAudioContext = FakeCtx;
	return { oscillators, resume };
}

/** Re-import the singleton with a clean module registry. */
async function freshService() {
	vi.resetModules();
	return (await import('./notificationSound')).notificationSound;
}

beforeEach(() => {
	vi.useRealTimers();
	localStorage.clear();
	delete (window as unknown as { AudioContext?: unknown }).AudioContext;
	delete (window as unknown as { webkitAudioContext?: unknown }).webkitAudioContext;
});

describe('enable/persist', () => {
	it('is enabled by default', async () => {
		installFakeAudio();
		const ns = await freshService();
		expect(ns.isEnabled()).toBe(true);
	});

	it('loads a disabled state from localStorage', async () => {
		localStorage.setItem(STORAGE_KEY, 'false');
		installFakeAudio();
		const ns = await freshService();
		expect(ns.isEnabled()).toBe(false);
	});

	it('persists the enable state to localStorage', async () => {
		installFakeAudio();
		const ns = await freshService();
		ns.setEnabled(false);
		expect(ns.isEnabled()).toBe(false);
		expect(localStorage.getItem(STORAGE_KEY)).toBe('false');
	});
});

describe('playback', () => {
	it('does not synthesize audio when disabled', async () => {
		const { oscillators } = installFakeAudio();
		const ns = await freshService();
		ns.setEnabled(false);
		ns.play('complete');
		expect(oscillators.length).toBe(0);
	});

	it('plays the complete chime (3 notes) when enabled', async () => {
		const { oscillators } = installFakeAudio();
		const ns = await freshService();
		ns.play('complete');
		expect(oscillators.length).toBe(3);
	});

	it('plays the question chime (2 notes)', async () => {
		const { oscillators } = installFakeAudio();
		const ns = await freshService();
		ns.play('question');
		expect(oscillators.length).toBe(2);
	});

	it('plays for a permission alert', async () => {
		const { oscillators } = installFakeAudio();
		const ns = await freshService();
		ns.play('permission');
		expect(oscillators.length).toBe(2);
	});

	it('resumes a suspended context before playing (so it works in background tabs)', async () => {
		const { resume, oscillators } = installFakeAudio('suspended');
		const ns = await freshService();
		ns.play('complete');
		expect(resume).toHaveBeenCalled();
		await Promise.resolve();
		expect(oscillators.length).toBe(3);
	});

	it('does not schedule or consume cooldown when resuming audio fails', async () => {
		const { resume, oscillators } = installFakeAudio('suspended');
		resume.mockRejectedValueOnce(new Error('Autoplay blocked'));
		const ns = await freshService();
		ns.playComplete();
		await Promise.resolve();
		await Promise.resolve();
		expect(oscillators.length).toBe(0);
		ns.playComplete();
		await Promise.resolve();
		expect(oscillators.length).toBe(3);
	});

	it('no-ops safely when the Web Audio API is unavailable', async () => {
		// No fake audio installed → window.AudioContext is undefined.
		const ns = await freshService();
		expect(() => ns.play('complete')).not.toThrow();
	});

	it('drops completion sounds at 4999ms and plays at the 5000ms boundary through both entry points', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(10_000);
		const { oscillators } = installFakeAudio();
		const ns = await freshService();

		ns.play('complete');
		expect(oscillators.length).toBe(3);

		vi.advanceTimersByTime(4999);
		ns.playComplete();
		expect(oscillators.length).toBe(3);

		vi.advanceTimersByTime(1);
		ns.play('complete');
		expect(oscillators.length).toBe(6);
	});

	it('does not throttle question sounds during the completion cooldown', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(10_000);
		const { oscillators } = installFakeAudio();
		const ns = await freshService();

		ns.playComplete();
		ns.playQuestion();
		ns.play('permission');
		expect(oscillators.length).toBe(7);
	});

	it('does not consume the completion cooldown when audio is disabled', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(10_000);
		const { oscillators } = installFakeAudio();
		const ns = await freshService();

		ns.setEnabled(false);
		ns.playComplete();
		ns.setEnabled(true);
		ns.playComplete();
		expect(oscillators.length).toBe(3);
	});

	it('does not consume the completion cooldown when Web Audio is unavailable', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(10_000);
		const ns = await freshService();

		ns.playComplete();
		const { oscillators } = installFakeAudio();
		ns.playComplete();
		expect(oscillators.length).toBe(3);
	});
});

describe('autoplay unlock', () => {
	it('unlock() resumes a suspended context', async () => {
		const { resume } = installFakeAudio('suspended');
		const ns = await freshService();
		ns.unlock();
		expect(resume).toHaveBeenCalled();
	});

	it('unlocks on the first user gesture', async () => {
		const { resume } = installFakeAudio('suspended');
		await freshService(); // constructor attaches gesture listeners
		window.dispatchEvent(new Event('pointerdown'));
		expect(resume).toHaveBeenCalled();
	});
});
