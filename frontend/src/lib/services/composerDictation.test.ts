import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Editor } from '@tiptap/core';
import { createChatEditor } from '$lib/components/chat/tiptap/editorConfig';
import { get } from 'svelte/store';
import type { SttStreamHandlers } from './sttStream';

const mocks = vi.hoisted(() => ({ start: vi.fn(), capability: vi.fn(), settings: vi.fn(), save: vi.fn(), mirror: vi.fn() }));
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api/voice', () => ({ loadVoiceSettings: mocks.settings }));
vi.mock('./keyboard', () => ({ registerShortcut: vi.fn() }));
vi.mock('$lib/stores/hotkeyPreferences.svelte', () => ({ hotkeyPreferences: {} }));
vi.mock('$lib/utils/earcon', () => ({ playEarcon: vi.fn(), primeEarcons: vi.fn(), setEarconsEnabled: vi.fn() }));
vi.mock('./sttStream', () => ({ fetchSttCapability: mocks.capability, startSttStream: mocks.start, SttUnsupportedError: class extends Error {} }));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return { activeSession: { ...writable({sessionId:'a'}), saveDraftForSession: mocks.save, setDraft: vi.fn() } };
});
vi.mock('$lib/stores/session/draft-persistence', () => ({ writeDraft: mocks.mirror }));
import { bindSessionOwner } from '$lib/stores/session-owner';
import { cancelVoiceInput, globalSttState, triggerToggle } from './globalStt';
import { startComposerDictation, detachComposerDictation, attachComposerDictation, composerDictation } from './composerDictation';

describe('session-owned dictation through the capture producer', () => {
	let editors: Editor[];
	let handlers: SttStreamHandlers;
	let stop: ReturnType<typeof vi.fn>;
	function editor(text: string) {
		const result = createChatEditor({element:document.createElement('div'), initialContent:text});
		result.commands.setTextSelection(text.length + 1);
		editors.push(result);
		return result;
	}
	beforeEach(() => {
		vi.useFakeTimers(); editors = [];
		mocks.settings.mockResolvedValue({voice_input_mode:'toggle'});
		mocks.capability.mockResolvedValue({supported:true, partials:true});
		stop = vi.fn().mockResolvedValue('private final');
		mocks.start.mockImplementation(async (callbacks: SttStreamHandlers) => {
			handlers = callbacks; callbacks.onCapture?.();
			return {stop, abort:vi.fn(), getText:vi.fn()};
		});
	});
	afterEach(() => { cancelVoiceInput(); editors.forEach(e => e.destroy()); vi.useRealTimers(); vi.clearAllMocks(); });
	async function start(e: Editor) { startComposerDictation(e, 'a'); await vi.advanceTimersByTimeAsync(0); }
	it('keeps recording through navigation, then delivers the final text only to the original draft', async () => {
		const a = editor('Draft A'); await start(a);
		handlers.onTranscript?.('private');
		a.commands.insertContentAt(1, 'My ');
		detachComposerDictation(a);
		a.commands.setContent('<p>Draft B</p>');
		handlers.onTranscript?.('private continued');
		expect(a.getText()).toBe('Draft B');
		expect(get(globalSttState).isRecording).toBe(true);
		expect(get(composerDictation)).toEqual({sessionId:'a', attached:false});
		triggerToggle(); await vi.advanceTimersByTimeAsync(0);
		expect(mocks.save).toHaveBeenLastCalledWith('a', expect.stringContaining('My Draft A private final'));
		expect(a.getText()).toBe('Draft B');
		expect(get(composerDictation)).toBeNull();
		expect(get(globalSttState).isTranscribing).toBe(false);
	});
	it('survives editor destruction during preparation and reattaches without duplicating speech', async () => {
		let ready!: () => void;
		mocks.start.mockImplementation(callbacks => new Promise(resolve => {
			handlers = callbacks;
			ready = () => { callbacks.onCapture?.(); resolve({stop, abort:vi.fn(), getText:vi.fn()}); };
		}));
		const a = editor('Draft A'); await start(a);
		detachComposerDictation(a); a.destroy();
		ready(); await vi.advanceTimersByTimeAsync(0);
		handlers.onTranscript?.('private');
		const back = editor('stale'); attachComposerDictation(back, 'a');
		expect(back.getText()).toBe('Draft A private');
		back.commands.insertContentAt(1, 'My ');
		handlers.onTranscript?.('private continued');
		expect(back.getText()).toBe('My Draft A private continued');
		cancelVoiceInput();
		expect(back.getText()).toBe('My Draft A');
	});
	it('finishes safely after the source editor is destroyed during transcription', async () => {
		let finish!: (text:string) => void;
		stop.mockImplementation(() => new Promise(resolve => {finish=resolve;}));
		const a = editor('Draft A'); await start(a);
		triggerToggle(); await vi.advanceTimersByTimeAsync(0);
		expect(get(globalSttState).isTranscribing).toBe(true);
		detachComposerDictation(a); a.destroy(); finish('final words');
		await vi.advanceTimersByTimeAsync(0);
		expect(mocks.save).toHaveBeenLastCalledWith('a', expect.stringContaining('Draft A final words'));
		expect(get(globalSttState).isTranscribing).toBe(false);
	});
	it('releases the draft controller on a preparation failure', async () => {
		mocks.settings.mockRejectedValue(new Error('offline'));
		await start(editor('Draft A'));
		expect(get(composerDictation)).toBeNull();
		expect(get(globalSttState).isPreparing).toBe(false);
	});
	it('stops on account changes without restoring private text into the next account', async () => {
		const a = editor('private draft'); await start(a);
		detachComposerDictation(a); mocks.save.mockClear(); mocks.mirror.mockClear();
		bindSessionOwner('first', 'second');
		handlers.onTranscript?.('late secret');
		expect(mocks.save).not.toHaveBeenCalled();
		expect(mocks.mirror).not.toHaveBeenCalled();
		expect(get(composerDictation)).toBeNull();
		expect(get(globalSttState).isRecording).toBe(false);
	});
});
