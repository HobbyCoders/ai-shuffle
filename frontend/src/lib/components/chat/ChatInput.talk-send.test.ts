/**
 * The composer's circles: attach left of the text; dictate, then one slot
 * that is Talk while there is nothing to send and Send once there is, then
 * Stop while work runs. Attach, dictate and the call are not in the controls
 * row any more.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { initialState, reduce } from '$lib/stores/session/v2/reducer';
import '$lib/test-stubs/composer-dom';

const voice = vi.hoisted(() => ({ enabled: true }));
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
vi.mock('$lib/api/voice', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, isVoiceEnabled: vi.fn(async () => voice.enabled) };
});

import ChatInput from './ChatInput.svelte';
import VoiceCallButton from './VoiceCallButton.svelte';

Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

beforeEach(async () => {
	voice.enabled = true;
	sessionStorage.clear();
	activeSession.newChat();
	activeSession.setDraft('');
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

/** The visible circles after dictate, left to right, by accessible name. */
function circles(container: HTMLElement): string[] {
	const cluster = container.querySelector('.send-cluster')!;
	return [...cluster.querySelectorAll<HTMLButtonElement>('button')]
		.filter((b) => !b.closest('[hidden]') && !b.closest('.stash'))
		.map((b) => b.getAttribute('aria-label') ?? '');
}

async function type(container: HTMLElement, text: string) {
	const editor = container.querySelector<HTMLElement>('.tiptap')!;
	editor.focus();
	editor.innerHTML = `<p>${text}</p>`;
	await fireEvent.input(editor, { inputType: 'insertText', data: text.slice(-1) });
	await tick();
}

function startTurn() {
	Object.assign(activeSession.current, { sessionId: 'local-working', mode: 'code', profile: 'coding', draft: '' });
	const live = reduce(initialState(), { type: 'turn.started', id: 'started', seq: 1,
		session_id: 'local-working', turn_id: 'live-turn' });
	Object.assign(activeSession.current, { isStreaming: live.isStreaming, liveTurnId: live.liveTurnId });
	activeSession.setDraft('');
}

describe('Talk turns into Send', () => {
	it('shows Talk, not Send, in an empty composer, and Send once there is text', async () => {
		const { container } = render(ChatInput);
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in a new conversation']));
		expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();

		await type(container, 'Fix the flaky test');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Send']));
		expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(false);
		// Talk stays mounted (hidden), so a keystroke never tears down a starting call.
		expect(container.querySelector('.talk-slot[hidden] .call-circle')).toBeTruthy();

		await type(container, '');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in a new conversation']));
	});

	it('falls back to Send where no call can start', async () => {
		voice.enabled = false;
		const { container } = render(ChatInput);
		await tick();
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Send']));
		expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('keeps Stop as the last circle while work runs: Talk with no draft, Send with one', async () => {
		startTurn();
		const { container } = render(ChatInput);
		await waitFor(() => expect(circles(container)).toEqual([
			'Start voice input', 'Start a call in this conversation', 'Stop generating'
		]));

		await type(container, 'Also update the docs');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Send', 'Stop generating']));
	});

	it('names the agent on a new session', async () => {
		render(VoiceCallButton, { props: { variant: 'circle', agentName: 'Overseer' } });
		const talk = await screen.findByRole('button', { name: 'Talk to Overseer' });
		expect(talk.classList.contains('call-circle')).toBe(true);
	});
});

describe('the composer circles', () => {
	it('puts attach left of the text and keeps attach, dictate and the call out of the controls row', async () => {
		const { container } = render(ChatInput);
		const attach = screen.getByRole('button', { name: 'Attach files' });
		const editor = container.querySelector('.textarea-wrapper')!;
		expect(attach.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(attach.closest('.input-area')).toBeTruthy();
		const dock = container.querySelector('.controls-dock')!;
		expect(dock.querySelector('.composer-attach, .attach-btn, .voice-btn, .voice-call-btn, .call-circle')).toBeNull();
	});
});
