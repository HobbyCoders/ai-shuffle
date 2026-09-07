import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
import { cancelVoiceInput } from '$lib/services/globalStt';
import VoiceRecorder from './VoiceRecorder.svelte';

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
	it.each(['button', 'Escape'])('Discard via %s removes only speech', async (action) => {
		await start();
		editor.commands.insertContentAt(1, 'My ');
		if (action === 'button') await fireEvent.click(screen.getByRole('button', { name: 'Discard voice input' }));
		else await fireEvent.keyDown(window, { key: 'Escape' });
		expect(editor.getText()).toBe('My Draft A');
		handlers.onTranscript?.('late words');
		expect(editor.getText()).toBe('My Draft A');
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
