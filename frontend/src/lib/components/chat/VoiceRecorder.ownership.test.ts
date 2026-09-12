import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('$lib/api/voice', () => ({ loadVoiceSettings: vi.fn().mockResolvedValue({voice_input_mode:'auto', voice_input_silence_seconds:5}) }));
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { createLiveDictation } from './tiptap/liveDictation';
import type { SttStreamHandlers } from '$lib/services/sttStream';

const mocks = vi.hoisted(() => ({ start: vi.fn(), capability: vi.fn() }));
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/services/keyboard', () => ({ registerShortcut: vi.fn() }));
vi.mock('$lib/stores/hotkeyPreferences.svelte', () => ({ hotkeyPreferences: {} }));
vi.mock('$lib/utils/earcon', () => ({ playEarcon: vi.fn(), primeEarcons: vi.fn(), setEarconsEnabled: vi.fn() }));
vi.mock('$lib/services/sttStream', () => ({ fetchSttCapability: mocks.capability, startSttStream: mocks.start, SttUnsupportedError: class extends Error {} }));
import { cancelVoiceInput, globalSttState } from '$lib/services/globalStt';
import { get } from 'svelte/store';
import { startComposerDictation, detachComposerDictation, composerDictation } from '$lib/services/composerDictation';
import VoiceRecorder from './VoiceRecorder.svelte';
import TiptapEditor from './TiptapEditor.svelte';

describe('rendered recorder with live editor ownership', () => {
	let editor: Editor;
	let dictation: ReturnType<typeof createLiveDictation>;
	let handlers: SttStreamHandlers;
	let owner: string;
	beforeEach(() => {
		vi.useFakeTimers();
		owner = 'session-a';
		editor = new Editor({ extensions: [StarterKit], content: '<p>Draft A</p>' });
		editor.commands.setTextSelection(8);
		dictation = createLiveDictation(editor);
		mocks.capability.mockResolvedValue({ supported: true, partials: true });
		mocks.start.mockImplementation(async (callbacks: SttStreamHandlers) => {
			handlers = callbacks;
			callbacks.onCapture?.();
			return { stop: vi.fn().mockResolvedValue('final'), abort: vi.fn(), getText: vi.fn() };
		});
		render(VoiceRecorder, {
			onTranscription: vi.fn(), onLiveText: dictation.write, onLiveCommit: dictation.commit,
			onLiveDiscard: dictation.discard, getOwner: () => owner
		});
	});
	afterEach(() => {
		cancelVoiceInput(); cleanup(); dictation.destroy(); editor.destroy();
		vi.useRealTimers(); vi.clearAllMocks();
	});
	async function start() {
		await fireEvent.click(screen.getByRole('button', { name: 'Start voice input' }));
		await vi.advanceTimersByTimeAsync(0);
		handlers.onTranscript?.('private words');
		await tick();
	}
	it('hands off dictation inside the editor teardown before losing its view', async () => {
		cleanup(); editor.destroy();
		const mounted = render(TiptapEditor, {initialContent:'original',onBeforeDestroy:detachComposerDictation});
		await tick(); editor = mounted.component.getEditor()!;
		startComposerDictation(editor, 'session-a'); await tick(); await tick();
		handlers.onTranscript?.('first words');
		mounted.unmount(); await tick();
		expect(get(composerDictation)).toEqual({sessionId:'session-a',attached:false});
		expect(() => handlers.onTranscript?.('continued after destruction')).not.toThrow();
		cancelVoiceInput({discard:false});
		expect(get(globalSttState).isRecording).toBe(false);
	});
	it('does not show another draft as recording and survives recorder teardown', async () => {
		cleanup();
		render(VoiceRecorder, {sessionId:'session-a', onTranscription:vi.fn(), onStart:()=>startComposerDictation(editor,'session-a')});
		await start();
		detachComposerDictation(editor);
		cleanup();
		editor.destroy();
		render(VoiceRecorder, {sessionId:'session-b', onTranscription:vi.fn(), onStart:vi.fn()});
		await tick();
		const other = screen.getByRole('button', {name:'Start voice input'}) as HTMLButtonElement;
		expect(other.classList.contains('recording')).toBe(false);
		expect(other.classList.contains('transcribing')).toBe(false);
		expect(other.disabled).toBe(true);
		expect(get(globalSttState).isRecording).toBe(true);
		expect(get(composerDictation)).toEqual({sessionId:'session-a',attached:false});
		handlers.onTranscript?.('continued after unmount');
		cancelVoiceInput({discard:false});
		await tick();
		expect(other.disabled).toBe(false);
	});
	it('Escape discards only speech without a separate discard button', async () => {
		await start();
		editor.commands.insertContentAt(1, 'My ');
		expect(screen.queryByRole('button', { name: 'Discard voice input' })).toBeNull();
		const stopButton = screen.getByRole('button', { name: 'Stop voice input' });
		expect(stopButton.classList.contains('expanded')).toBe(true);
		expect(stopButton.querySelectorAll('.waveform-bar')).toHaveLength(20);
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(editor.getText()).toBe('My Draft A');
		handlers.onTranscript?.('late words');
		expect(editor.getText()).toBe('My Draft A');
	});
	it('clicking the waveform finishes dictation and restores the idle button', async () => {
		let finish!: (text: string) => void;
		mocks.start.mockImplementationOnce(async (callbacks: SttStreamHandlers) => {
			handlers = callbacks;
			callbacks.onCapture?.();
			return {
				stop: () => new Promise<string>((resolve) => { finish = resolve; }),
				abort: vi.fn(), getText: vi.fn()
			};
		});
		await start();
		await fireEvent.click(screen.getByRole('button', { name: 'Stop voice input' }));
		await vi.advanceTimersByTimeAsync(0);
		const processingButton = screen.getByRole('button', { name: 'Start voice input' }) as HTMLButtonElement;
		expect(processingButton.classList.contains('transcribing')).toBe(true);
		expect(processingButton.disabled).toBe(true);
		expect(screen.queryByRole('button', { name: 'Discard voice input' })).toBeNull();
		finish('private words');
		await vi.advanceTimersByTimeAsync(0);
		const startButton = screen.getByRole('button', { name: 'Start voice input' });
		expect(startButton.classList.contains('expanded')).toBe(false);
		expect(startButton.querySelector('.voice-waveform')).toBeNull();
		expect(screen.queryByRole('button', { name: 'Discard voice input' })).toBeNull();
		expect(editor.getText()).toBe('Draft A private words');
	});
	it('rejects a transcript immediately on owner change, even before navigation effects settle', async () => {
		await start();
		expect(editor.getText()).toBe('Draft A private words');
		owner = 'session-b';
		handlers.onTranscript?.('private words continued');
		expect(editor.getText()).toBe('Draft A private words');
		// ChatInput settles the leaving editor before replacing its document.
		dictation.commit();
		cancelVoiceInput({ discard: false });
		editor.commands.setContent('<p>Draft B</p>');
		handlers.onTranscript?.('late final');
		expect(editor.getText()).toBe('Draft B');
	});
});
