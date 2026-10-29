import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { appData } from '$lib/stores/app-data.svelte';
import { notifications, notify } from '$lib/stores/notifications';
import '$lib/test-stubs/composer-dom';

// A send refused on the new-session screen (no agent picked yet) used to put
// "Select an agent first." in the notification bell only: the bell's count
// went up (inside the closed drawer on a phone) and the tap on Send looked
// dead. Found by the phone-composer journey. The refusal must show at once,
// as a live banner, and keep the typed text.
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

const TEXT = 'hello without an agent';
let sends: MockInstance;
const banners = () => notifications.current.live.filter((n) => n.surface === 'banner');

beforeEach(async () => {
	sessionStorage.clear();
	for (const banner of notifications.current.live) notify.dismissLive(banner.id);
	activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: null, mode: 'chat', profile: '', project: null, error: null, worktreeError: null });
	// One agent exists; none is picked for this new chat.
	appData.current.profiles = [{ id: 'dev-agent', name: 'Dev agent' }] as never;
	activeSession.setDraft(TEXT);
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	appData.current.profiles = [];
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

describe('a send refused for a missing agent', () => {
	it('says why in a live banner at once and keeps the text', async () => {
		const view = render(ChatInput, { props: { newSession: true } });
		const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(banners().map((n) => n.message)).toContain('Select an agent first.'));
		const banner = banners().find((n) => n.message === 'Select an agent first.')!;
		expect(banner.severity).toBe('warning');
		// Seen now, then gone on its own; never only a bell item.
		expect(banner.duration_ms).toBeGreaterThan(0);
		expect(sends).not.toHaveBeenCalled();
		expect(editor.textContent).toBe(TEXT);
	});
});
