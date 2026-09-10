import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import type { SttStreamHandlers } from '$lib/services/sttStream';

const mocks = vi.hoisted(() => ({ start: vi.fn(), capability: vi.fn() }));
vi.mock('$lib/services/sttStream', () => ({
	fetchSttCapability: mocks.capability, startSttStream: mocks.start,
	SttUnsupportedError: class extends Error {}
}));
vi.mock('$lib/utils/earcon', () => ({ playEarcon: vi.fn(), primeEarcons: vi.fn(), setEarconsEnabled: vi.fn() }));
vi.mock('$lib/stores/hotkeyPreferences.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { hotkeyPreferences: writable({
		globalSttEnabled: true, globalSttHotkey: { key: 'm', cmdOrCtrl: true, shift: true }
	}) };
});
import { cancelVoiceInput, globalSttState, initGlobalStt, toggleVoiceInput, triggerToggle } from '$lib/services/globalStt';
import GlobalVoiceInput from './GlobalVoiceInput.svelte';
import VoiceRecorder from '../chat/VoiceRecorder.svelte';

describe('global voice input', () => {
	let handlers: SttStreamHandlers;
	let dispose: () => void;
	let stop: ReturnType<typeof vi.fn>;
	let writeText: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		vi.useFakeTimers();
		mocks.capability.mockResolvedValue({ supported: true, model_name: 'Live transcribe', partials: true });
		stop = vi.fn().mockResolvedValue('Hello world.');
		mocks.start.mockImplementation(async (callbacks: SttStreamHandlers) => {
			handlers = callbacks;
			callbacks.onCapture?.();
			return { stop, abort: vi.fn(), getText: vi.fn() };
		});
		writeText = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal('navigator', { clipboard: { writeText } });
		Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(false) });
		dispose = initGlobalStt();
	});

	afterEach(() => {
		cancelVoiceInput();
		dispose();
		cleanup();
		document.body.replaceChildren();
		vi.useRealTimers();
		vi.unstubAllGlobals();
		vi.clearAllMocks();
	});

	async function activate() {
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'm', ctrlKey: true, shiftKey: true, bubbles: true }));
		await vi.advanceTimersByTimeAsync(0);
	}

	it('shows from the hotkey without a composer, preserves the caret, and closes after automatic transcription', async () => {
		let finish!: (text: string) => void;
		stop.mockImplementation(() => new Promise<string>(resolve => { finish = resolve; }));
		render(GlobalVoiceInput);
		expect(screen.queryByRole('complementary')).toBeNull();
		const field = document.createElement('textarea');
		field.value = 'Before after';
		document.body.append(field);
		field.focus();
		field.setSelectionRange(7, 7);
		const onInput = vi.fn();
		field.addEventListener('input', onInput);

		await activate();
		expect(screen.getByRole('complementary', { name: 'Global voice input' })).toBeTruthy();
		expect(screen.getByRole('status').textContent).toContain('Listening');
		expect(document.activeElement).toBe(field);
		handlers.onSpeech?.(true);
		handlers.onTranscript?.('Hello');
		await tick();
		expect(screen.getByText('Hello')).toBeTruthy();
		handlers.onSpeech?.(false);
		// Server VAD has already waited 700ms before reporting silence.
		await vi.advanceTimersByTimeAsync(4299);
		expect(stop).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(stop).toHaveBeenCalledTimes(1);
		expect(screen.getByRole('status').textContent).toContain('Transcribing');
		// Clicking a composer microphone during the drain must not take over
		// the destination of dictation that was started with the global hotkey.
		const otherSink = vi.fn();
		toggleVoiceInput(otherSink);
		finish('Hello world.');
		await vi.advanceTimersByTimeAsync(0);
		expect(field.value).toBe('Before Hello world.after');
		expect(onInput).toHaveBeenCalledTimes(1);
		expect(writeText).toHaveBeenCalledWith('Hello world.');
		expect(otherSink).not.toHaveBeenCalled();
		expect(screen.queryByRole('complementary')).toBeNull();
	});

	it('waits for resumed speech and keeps listening beyond the initial no-speech deadline', async () => {
		await activate();
		handlers.onSpeech?.(true);
		await vi.advanceTimersByTimeAsync(13000);
		expect(stop).not.toHaveBeenCalled();
		handlers.onSpeech?.(false);
		await vi.advanceTimersByTimeAsync(1000);
		handlers.onSpeech?.(true);
		await vi.advanceTimersByTimeAsync(2000);
		expect(stop).not.toHaveBeenCalled();
		handlers.onSpeech?.(false);
		// Server VAD has already waited 700ms before reporting silence.
		await vi.advanceTimersByTimeAsync(4299);
		expect(stop).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(stop).toHaveBeenCalledTimes(1);
	});

	it('shows setup and recoverable errors outside the composer', async () => {
		let fail!: (error: Error) => void;
		mocks.start.mockImplementation(() => new Promise((_, reject) => { fail = reject; }));
		render(GlobalVoiceInput);
		await activate();
		expect(screen.getByRole('status').textContent).toContain('Getting microphone ready');
		fail(new Error('Microphone access denied'));
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.getByRole('alert').textContent).toBe('Microphone access denied');
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss voice input error' }));
		expect(screen.queryByRole('complementary')).toBeNull();
	});

	it.each(['Escape', 'button'])('discards with %s and cancels the automatic finish', async (control) => {
		render(GlobalVoiceInput);
		await activate();
		handlers.onSpeech?.(true);
		handlers.onSpeech?.(false);
		if (control === 'Escape') await fireEvent.keyDown(window, { key: 'Escape' });
		else await fireEvent.click(screen.getByRole('button', { name: 'Discard voice input' }));
		await vi.advanceTimersByTimeAsync(12000);
		expect(screen.queryByRole('complementary')).toBeNull();
		expect(stop).not.toHaveBeenCalled();
		expect(writeText).not.toHaveBeenCalled();
	});

	it('ends a silent activation and surfaces the no-speech result', async () => {
		stop.mockResolvedValue('');
		render(GlobalVoiceInput);
		await activate();
		await vi.advanceTimersByTimeAsync(12000);
		expect(get(globalSttState).isRecording).toBe(false);
		expect(screen.getByRole('alert').textContent).toBe('No speech detected');
	});

	it('leaves composer dictation inline and delivers its final text automatically', async () => {
		render(GlobalVoiceInput);
		const sink = vi.fn();
		toggleVoiceInput(sink);
		await vi.advanceTimersByTimeAsync(0);
		expect(screen.queryByRole('complementary')).toBeNull();
		handlers.onSpeech?.(true);
		handlers.onSpeech?.(false);
		// Server VAD has already waited 700ms before reporting silence.
		await vi.advanceTimersByTimeAsync(4299);
		expect(stop).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(sink).toHaveBeenCalledWith('Hello world.');
		expect(writeText).not.toHaveBeenCalled();
	});

	it('keeps global recording alive when navigating away from a mounted composer', async () => {
		const composer = render(VoiceRecorder, { onTranscription: vi.fn() });
		triggerToggle();
		await vi.advanceTimersByTimeAsync(0);
		composer.unmount();
		expect(get(globalSttState).isRecording).toBe(true);
		handlers.onSpeech?.(true);
		handlers.onSpeech?.(false);
		// Server VAD has already waited 700ms before reporting silence.
		await vi.advanceTimersByTimeAsync(4299);
		expect(stop).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(writeText).toHaveBeenCalledWith('Hello world.');
	});
});
