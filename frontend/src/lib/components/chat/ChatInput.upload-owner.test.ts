import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { api } from '$lib/api/client';
import { readDraft } from '$lib/stores/session/draft-persistence';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
import { LARGE_PASTE_BYTES } from './tiptap/large-paste';
import '$lib/test-stubs/composer-dom';

// #1659. Until the page knows who runs the open chat (a reload, a deep link,
// a restored tab, `/live` slow or failing), its legacy project id may name an
// unrelated project on this worker. No upload may go to that folder then.
vi.mock('./ContextPillBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
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

const row = (is_remote: boolean) => ({
	id: 'owned', mode: 'code', project_id: 'app', project_v2_id: 'pv2-theirs', permission_mode: 'default', is_remote,
});
const uploaded = { path: 'uploads/a.txt', filename: 'a.txt', full_path: '/x/uploads/a.txt', size: 1 };

beforeEach(async () => {
	localStorage.clear();
	sessionStorage.clear();
	sessionEphemeralCache.clear();
	activeSession.newChat();
	// What a reload restores from a saved snapshot before `/live` answers.
	Object.assign(activeSession.current, {
		sessionId: 'owned', project: 'app', projectV2Id: 'pv2-theirs', mode: 'code', profile: 'coding',
		isRemote: undefined, isStreaming: false, liveTurnId: null, error: null, worktreeError: null, messages: [], draft: ''
	});
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	vi.useRealTimers();
	Object.assign(activeSession.current, { sessionId: null, isRemote: undefined });
	vi.restoreAllMocks();
});

async function pickFile(container: HTMLElement) {
	const input = container.querySelector('input[type="file"]') as HTMLInputElement;
	expect(input).toBeTruthy();
	Object.defineProperty(input, 'files', { configurable: true, value: [new File(['x'], 'a.txt', { type: 'text/plain' })] });
	await fireEvent.change(input);
}

describe('a composer upload while the chat\'s owner is unknown (#1659)', () => {
	it('waits, then goes through the session upload when another worker runs the chat', async () => {
		const upload = vi.spyOn(api, 'uploadFile').mockResolvedValue(uploaded as never);
		const { container } = render(ChatInput);
		await pickFile(container);
		await new Promise(r => setTimeout(r, 50));
		expect(upload).not.toHaveBeenCalled();
		activeSession.reconcileOwner([row(true)]);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		expect(upload.mock.calls[0][0]).toBe('/sessions/owned/upload');
	});

	it('waits, then uploads into the local project when this worker runs the chat', async () => {
		const upload = vi.spyOn(api, 'uploadFile').mockResolvedValue(uploaded as never);
		const { container } = render(ChatInput);
		await pickFile(container);
		activeSession.reconcileOwner([row(false)]);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		expect(upload.mock.calls[0][0]).toBe('/projects/app/upload');
	});

	it('uploads at once when the owner is already known', async () => {
		activeSession.current.isRemote = false;
		const upload = vi.spyOn(api, 'uploadFile').mockResolvedValue(uploaded as never);
		const { container } = render(ChatInput);
		await pickFile(container);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		expect(upload.mock.calls[0][0]).toBe('/projects/app/upload');
	});

	it('refuses, uploading nothing, when the owner never becomes known (`/live` failed)', async () => {
		vi.useFakeTimers({ shouldAdvanceTime: true });
		const upload = vi.spyOn(api, 'uploadFile').mockResolvedValue(uploaded as never);
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const { container, findByText } = render(ChatInput);
		await pickFile(container);
		await vi.advanceTimersByTimeAsync(5_000);
		expect(await findByText(/couldn’t confirm which worker runs this chat/)).toBeTruthy();
		expect(upload).not.toHaveBeenCalled();
	});
});

// #1659 S5. What a composer action produces belongs to the chat it started in:
// if another chat is open by the time it settles, it goes into the first
// chat's stored draft, never into the editor now showing the other chat.
describe('a paste or attachment that settles after the chat changed (#1659)', () => {
	function paste(target: HTMLElement, text: string) {
		const event = new Event('paste', { bubbles: true, cancelable: true }) as Event & { clipboardData: unknown };
		event.clipboardData = { getData: (type: string) => (type === 'text/plain' ? text : ''), types: ['text/plain'], files: [], items: [] };
		target.dispatchEvent(event);
	}
	/** Open another chat (a notifying store update, as a switch makes). */
	function leaveForAnotherChat() {
		Object.assign(activeSession.current, { sessionId: 'chat-b', project: '', projectV2Id: null, isRemote: false });
		activeSession.setTitle('chat-b', 'B');
	}
	const storedDraft = (id: string) => sessionEphemeralCache.peekDraft(id) || readDraft(id) || '';

	it('a large paste waiting on the owner goes back into its own chat\'s draft when the user leaves', async () => {
		const upload = vi.spyOn(api, 'uploadFile').mockResolvedValue(uploaded as never);
		const { container } = render(ChatInput);
		const editor = container.querySelector('[role="textbox"]') as HTMLElement;
		const text = 'p'.repeat(LARGE_PASTE_BYTES);
		paste(editor, text);
		await new Promise(r => setTimeout(r, 50));
		leaveForAnotherChat();
		await waitFor(() => expect(storedDraft('owned')).toContain('pppp'));
		expect(upload).not.toHaveBeenCalled();
		expect(editor.textContent).not.toContain('pppp');
		expect(storedDraft('chat-b')).not.toContain('pppp');
	});

	it('an attachment whose upload finishes after the user left lands in its own chat\'s draft', async () => {
		activeSession.current.isRemote = false;
		let finish!: (value: unknown) => void;
		const upload = vi.spyOn(api, 'uploadFile').mockImplementation(() => new Promise(done => { finish = done; }) as never);
		const { container } = render(ChatInput);
		const editor = container.querySelector('[role="textbox"]') as HTMLElement;
		await pickFile(container);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		expect(upload.mock.calls[0][0]).toBe('/projects/app/upload');
		leaveForAnotherChat();
		finish(uploaded);
		await waitFor(() => expect(storedDraft('owned')).toContain('uploads/a.txt'));
		expect(editor.innerHTML).not.toContain('uploads/a.txt');
		expect(storedDraft('chat-b')).not.toContain('uploads/a.txt');
	});
});

