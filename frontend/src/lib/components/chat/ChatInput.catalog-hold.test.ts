import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { auth } from '$lib/stores/auth.svelte';
import { notifications } from '$lib/stores/notifications';
import '$lib/test-stubs/composer-dom';

// #1636 review: a new chat's remembered project and branch resolve against the
// project catalog. A send before it answers is held with the text in place,
// then goes out once the catalog has finished the composer, or asks.
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const TEXT = 'held until the projects load';
let sends: MockInstance;
const notices = () => notifications.current.history.map(n => n.message);

beforeEach(async () => {
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: null, mode: 'code', profile: 'coding', project: 'p', error: null, worktreeError: null });
	activeSession.setDraft(TEXT);
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

async function submit() {
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => expect(editor.textContent).toBe(TEXT));
	await fireEvent.submit(view.container.querySelector('form')!);
	return { ...view, editor };
}

describe('send held for the project catalog', () => {
	it('holds the text with a loading state, then sends once the catalog finishes the composer', async () => {
		let answer!: () => void;
		vi.spyOn(activeSession, 'composerAwaitingProjectCatalog', 'get').mockReturnValue(true);
		vi.spyOn(appData, 'whenProjectCatalogLoaded').mockReturnValue(new Promise<void>(resolve => { answer = resolve; }));
		vi.spyOn(appData, 'projectCatalogFailed', 'get').mockReturnValue(false);
		const resolveDefaults = vi.spyOn(activeSession, 'resolveComposerDefaultsAfterCatalog').mockImplementation(() => {});
		const { editor } = await submit();
		await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Loading projects…'));
		expect(screen.getByRole('button', { name: 'Loading projects…' })).toBeTruthy();
		expect(sends).not.toHaveBeenCalled();
		expect(editor.textContent).toBe(TEXT);
		answer();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(resolveDefaults).toHaveBeenCalledOnce();
		expect(resolveDefaults.mock.invocationCallOrder[0]).toBeLessThan(sends.mock.invocationCallOrder[0]);
		expect(sends.mock.calls[0][0]).toBe(TEXT);
		expect(screen.queryByText('Loading projects…')).toBeNull();
	});

	it('asks instead of sending when the catalog failed, and the next send goes out as composed', async () => {
		vi.spyOn(activeSession, 'composerAwaitingProjectCatalog', 'get').mockReturnValue(true);
		vi.spyOn(appData, 'whenProjectCatalogLoaded').mockResolvedValue();
		vi.spyOn(appData, 'projectCatalogFailed', 'get').mockReturnValue(true);
		vi.spyOn(activeSession, 'resolveComposerDefaultsAfterCatalog').mockImplementation(() => {});
		const { container, editor } = await submit();
		await waitFor(() => expect(notices()).toContain("Projects didn't load, so your usual project and branch may not be set. Check them, then send again."));
		expect(sends).not.toHaveBeenCalled();
		expect(editor.textContent).toBe(TEXT);
		await fireEvent.submit(container.querySelector('form')!);
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
	});

	it('does not hold once the catalog has answered', async () => {
		vi.spyOn(activeSession, 'composerAwaitingProjectCatalog', 'get').mockReturnValue(false);
		const wait = vi.spyOn(appData, 'whenProjectCatalogLoaded');
		await submit();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(wait).not.toHaveBeenCalled();
	});
});

describe('refused send notice', () => {
	it('is not suppressed by an earlier attempt\'s banner still showing', async () => {
		activeSession.current.error = 'Unable to connect. Please check your connection and try again.';
		sends.mockResolvedValue(false);
		const before = notices().filter(m => m === 'Message not sent. Try again.').length;
		const { editor } = await submit();
		await waitFor(() => expect(notices().filter(m => m === 'Message not sent. Try again.').length).toBe(before + 1));
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
	});

	it('leaves the reason to a banner this send set', async () => {
		activeSession.current.worktreeError = 'an earlier failure';
		sends.mockImplementation(async () => {
			// The refusal sets its own reason (setDraft publishes the change).
			Object.assign(activeSession.current, { worktreeError: 'Server error creating worktree' });
			activeSession.setDraft(activeSession.current.draft);
			return false;
		});
		const before = notices().filter(m => m === 'Message not sent. Try again.').length;
		const { editor } = await submit();
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		expect(notices().filter(m => m === 'Message not sent. Try again.').length).toBe(before);
	});
});
