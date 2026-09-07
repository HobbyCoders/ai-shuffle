import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import type { SttStreamHandlers } from './sttStream';

const mocks = vi.hoisted(() => ({ start: vi.fn(), capability: vi.fn() }));
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('./keyboard', () => ({ registerShortcut: vi.fn() }));
vi.mock('$lib/stores/hotkeyPreferences.svelte', () => ({ hotkeyPreferences: {} }));
vi.mock('$lib/utils/earcon', () => ({ playEarcon: vi.fn(), primeEarcons: vi.fn(), setEarconsEnabled: vi.fn() }));
vi.mock('./sttStream', () => ({ fetchSttCapability: mocks.capability, startSttStream: mocks.start, SttUnsupportedError: class extends Error {} }));
import { cancelVoiceInput, globalSttState, toggleVoiceInput } from './globalStt';

describe('composer live transcription delivery', () => {
	let handlers: SttStreamHandlers;
	let session: { stop: ReturnType<typeof vi.fn>; abort: ReturnType<typeof vi.fn>; getText: ReturnType<typeof vi.fn> };
	beforeEach(() => {
		vi.useFakeTimers();
		mocks.capability.mockResolvedValue({ supported: true, model_name: 'GPT Live Transcribe', partials: true });
		session = { stop: vi.fn().mockResolvedValue('Hello world.'), abort: vi.fn(), getText: vi.fn() };
		mocks.start.mockImplementation(async (callbacks: SttStreamHandlers) => { handlers = callbacks; callbacks.onCapture?.(); return session; });
	});
	afterEach(() => { cancelVoiceInput(); vi.useRealTimers(); vi.clearAllMocks(); });
	it('does not record a doomed batch upload for a live-only model without credentials', async () => {
		mocks.capability.mockResolvedValue({ supported: false, realtime_only: true,
			model_name: 'GPT Live Transcribe', reason: 'Add an OpenAI API key in Providers.' });
		const getUserMedia = vi.fn();
		vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
		try {
			toggleVoiceInput(vi.fn(), { onText: vi.fn(), onCommit: vi.fn() });
			await vi.advanceTimersByTimeAsync(0);
			expect(mocks.start).not.toHaveBeenCalled();
			expect(getUserMedia).not.toHaveBeenCalled();
			expect(get(globalSttState)).toMatchObject({ mode: 'idle', isPreparing: false,
				isRecording: false, error: 'Add an OpenAI API key in Providers.' });
		} finally { vi.unstubAllGlobals(); }
	});
	it('Discard invokes removal, never commits, and rejects late speech', async () => {
		const onText = vi.fn(); const onCommit = vi.fn(); const onDiscard = vi.fn();
		toggleVoiceInput(vi.fn(), { onText, onCommit, onDiscard });
		await vi.advanceTimersByTimeAsync(0);
		handlers.onTranscript?.('private words');
		cancelVoiceInput();
		handlers.onTranscript?.('late private words');
		expect(onDiscard).toHaveBeenCalledOnce();
		expect(onCommit).not.toHaveBeenCalled();
		expect(onText).toHaveBeenCalledTimes(1);
	});
	it('keeps no-VAD speech past 12 seconds and commits after 2.5 seconds of local silence', async () => {
		mocks.start.mockImplementation(async (callbacks: SttStreamHandlers) => {
			handlers = callbacks;
			callbacks.onReady?.({ model: 'gpt-live-transcribe', modelName: 'GPT Live Transcribe', partials: true, serverVad: false });
			callbacks.onCapture?.(); return session;
		});
		const onCommit = vi.fn();
		toggleVoiceInput(vi.fn(), { onText: vi.fn(), onCommit });
		await vi.advanceTimersByTimeAsync(0);
		handlers.onSpeech?.(true);
		await vi.advanceTimersByTimeAsync(13000);
		expect(get(globalSttState).isRecording).toBe(true);
		expect(session.stop).not.toHaveBeenCalled();
		handlers.onSpeech?.(false);
		await vi.advanceTimersByTimeAsync(2499);
		expect(session.stop).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(session.stop).toHaveBeenCalledOnce();
		expect(onCommit).toHaveBeenCalledOnce();
		expect(get(globalSttState).isRecording).toBe(false);
	});
	it('still stops a no-VAD session with no speech and permits explicit stop before silence', async () => {
		mocks.start.mockImplementation(async (callbacks: SttStreamHandlers) => {
			handlers = callbacks;
			callbacks.onReady?.({ model: 'gpt-live-transcribe', modelName: 'GPT Live Transcribe', partials: true, serverVad: false });
			callbacks.onCapture?.(); return session;
		});
		toggleVoiceInput(vi.fn(), { onText: vi.fn(), onCommit: vi.fn() });
		await vi.advanceTimersByTimeAsync(12000);
		expect(session.stop).toHaveBeenCalledOnce();
		toggleVoiceInput(vi.fn(), { onText: vi.fn(), onCommit: vi.fn() });
		await vi.advanceTimersByTimeAsync(0);
		handlers.onSpeech?.(true);
		toggleVoiceInput(vi.fn());
		await vi.advanceTimersByTimeAsync(0);
		expect(session.stop).toHaveBeenCalledTimes(2);
	});
	it('navigation settles visible speech into the old draft and rejects pending final', async () => {
		const onText = vi.fn(); const onCommit = vi.fn(); const onDiscard = vi.fn();
		toggleVoiceInput(vi.fn(), { onText, onCommit, onDiscard });
		await vi.advanceTimersByTimeAsync(0);
		handlers.onTranscript?.('original session');
		cancelVoiceInput({ discard: false });
		handlers.onTranscript?.('destination must not receive');
		expect(onCommit).toHaveBeenCalledOnce();
		expect(onDiscard).not.toHaveBeenCalled();
		expect(onText).toHaveBeenCalledExactlyOnceWith('original session');
	});
	it('delivers interim words while recording, settles final once, never appends a duplicate', async () => {
		const final = vi.fn(); const onText = vi.fn(); const onCommit = vi.fn();
		toggleVoiceInput(final, { onText, onCommit });
		await vi.advanceTimersByTimeAsync(0);
		handlers.onTranscript?.('Hello');
		expect(get(globalSttState).isRecording).toBe(true);
		expect(onText).toHaveBeenLastCalledWith('Hello');
		toggleVoiceInput(final);
		await vi.advanceTimersByTimeAsync(0);
		expect(onText).toHaveBeenLastCalledWith('Hello world.');
		expect(onCommit).toHaveBeenCalledTimes(1);
		expect(final).not.toHaveBeenCalled();
	});
	it('cancel during setup aborts the late session and ignores subsequent transcript events', async () => {
		let resolve!: (value: typeof session) => void;
		mocks.start.mockImplementation((callbacks: SttStreamHandlers) => { handlers = callbacks; return new Promise((r) => { resolve = r; }); });
		const onText = vi.fn();
		toggleVoiceInput(vi.fn(), { onText, onCommit: vi.fn() });
		await vi.advanceTimersByTimeAsync(0);
		cancelVoiceInput();
		resolve(session);
		await vi.advanceTimersByTimeAsync(0);
		handlers.onCapture?.(); handlers.onTranscript?.('stale words');
		expect(session.abort).toHaveBeenCalledOnce();
		expect(onText).not.toHaveBeenCalled();
		expect(get(globalSttState).isRecording).toBe(false);
	});
	it('cancel during final drain ignores late final text', async () => {
		let resolve!: (text: string) => void;
		session.stop.mockImplementation(() => new Promise((r) => { resolve = r; }));
		const onText = vi.fn();
		toggleVoiceInput(vi.fn(), { onText, onCommit: vi.fn() });
		await vi.advanceTimersByTimeAsync(0);
		toggleVoiceInput(vi.fn());
		cancelVoiceInput(); resolve('late final');
		await vi.advanceTimersByTimeAsync(0);
		expect(onText).not.toHaveBeenCalled();
	});
});
