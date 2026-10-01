import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
import { composerSuggestion } from '$lib/stores/session/composer-suggestion';
import '$lib/test-stubs/composer-dom';

// The rendered composer with a server-retained reply suggestion (#1402). The
// suggestion arrives through the real store's authoritative read; no model call.
const SUGGESTION = 'Check the deployment logs';
let hint: { session_id: string; turn_id: string; suggestion: string } | null = null;

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/composer-suggestion')
			? hint
			: url.includes('/auth/status')
				? { authenticated: true, has_any_credential: true, onboarding_complete: true }
				: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

let sends: MockInstance;
let turn = 0;

beforeEach(async () => {
	sessionStorage.clear();
	composerSuggestion.reset();
	activeSession.newChat();
	hint = { session_id: 'suggesting', turn_id: `turn-${++turn}`, suggestion: SUGGESTION };
	Object.assign(activeSession.current, {
		sessionId: 'suggesting', mode: 'code', profile: 'coding', draft: '',
		isStreaming: false, liveTurnId: null
	});
	activeSession.setDraft('');
	chatPreferences.updatePreference('suggestNextResponse', true);
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	chatPreferences.updatePreference('suggestNextResponse', false);
	vi.restoreAllMocks();
});

async function renderWithSuggestion() {
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(view.container.querySelector('.inline-suggestion')?.textContent?.trim()).toBe(SUGGESTION));
	return { ...view, editor };
}

describe('composer reply suggestion presentation', () => {
	it('shows the suggestion inline only, with no separate action row', async () => {
		const { container, editor } = await renderWithSuggestion();
		expect(screen.queryByRole('button', { name: /use reply/i })).toBeNull();
		expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull();
		expect(screen.queryByRole('group', { name: /reply suggestion/i })).toBeNull();
		expect(container.textContent).not.toMatch(/Use reply/i);
		// The hint sits inside the editor shell, never inside the editable draft.
		const hintEl = container.querySelector('.inline-suggestion')!;
		expect(hintEl.parentElement!.classList.contains('editor-shell')).toBe(true);
		expect(editor.contains(hintEl)).toBe(false);
		expect(activeSession.current.draft).not.toContain(SUGGESTION);
		// Any reintroduced suggestion-only control would change the button set.
		const buttons = () => screen.queryAllByRole('button').map(b => b.getAttribute('aria-label') ?? b.textContent?.trim());
		const withSuggestion = buttons();
		await fireEvent.keyDown(editor, { key: 'Escape' });
		await tick();
		expect(container.querySelector('.inline-suggestion')).toBeNull();
		expect(buttons()).toEqual(withSuggestion);
	});

	it('Tab accepts into the editor without sending', async () => {
		const { container, editor } = await renderWithSuggestion();
		await fireEvent.keyDown(editor, { key: 'Tab' });
		await tick();
		await waitFor(() => expect(editor.textContent).toBe(SUGGESTION));
		expect(container.querySelector('.inline-suggestion')).toBeNull();
		expect(sends).not.toHaveBeenCalled();
	});

	it('Escape dismisses without changing the draft', async () => {
		const { container, editor } = await renderWithSuggestion();
		await fireEvent.keyDown(editor, { key: 'Escape' });
		await tick();
		expect(container.querySelector('.inline-suggestion')).toBeNull();
		expect(editor.textContent).toBe('');
		expect(sends).not.toHaveBeenCalled();
	});

	it('double interaction on the composer accepts for pointer and touch users', async () => {
		const { editor } = await renderWithSuggestion();
		await fireEvent.doubleClick(editor);
		await waitFor(() => expect(editor.textContent).toBe(SUGGESTION));
		expect(sends).not.toHaveBeenCalled();
	});
});
