import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { api } from '$lib/api/client';
import { readDraft } from '$lib/stores/session/draft-persistence';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
import { LARGE_PASTE_BYTES } from './tiptap/large-paste';
import { bumpInitGeneration } from '$lib/stores/session/session-init';
import { ATTACHMENTS_REMOTE_UNLINKED } from './attach-disable';
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

// #1659 S7. A New chat whose own send creates its chat takes that id without
// a navigation: a paste or attachment started before is that chat's, not the
// New chat slot's. A real navigation (another chat, a fresh New chat) is a leave.
describe('a paste or attachment started on a New chat that takes its id (#1659)', () => {
	function paste(target: HTMLElement, text: string) {
		const event = new Event('paste', { bubbles: true, cancelable: true }) as Event & { clipboardData: unknown };
		event.clipboardData = { getData: (type: string) => (type === 'text/plain' ? text : ''), types: ['text/plain'], files: [], items: [] };
		target.dispatchEvent(event);
	}
	const storedDraft = (id: string | null) => sessionEphemeralCache.peekDraft(id ?? '__new_chat__') || readDraft(id) || '';
	/** The send's chat binds to the open New chat (no navigation). */
	function adopt(id: string) {
		Object.assign(activeSession.current, { sessionId: id, isRemote: false });
		activeSession.setDraft('');
	}
	/** The user opens another chat (a navigation). */
	function navigateTo(id: string) {
		bumpInitGeneration();
		Object.assign(activeSession.current, { sessionId: id, project: '', projectV2Id: null, isRemote: false });
	}
	/** A New chat whose first message is sent and waiting for its chat. */
	async function sendOnNewChat(): Promise<{ container: HTMLElement; editor: HTMLElement }> {
		Object.assign(activeSession.current, { sessionId: null, isRemote: undefined, isStreaming: false, liveTurnId: null });
		activeSession.setDraft('first message');
		const sends = vi.spyOn(activeSession, 'sendMessage').mockImplementation(() => new Promise<boolean>(() => {}));
		const { container } = render(ChatInput);
		const editor = container.querySelector('[role="textbox"]') as HTMLElement;
		await waitFor(() => expect(editor.textContent).toBe('first message'));
		await fireEvent.submit(container.querySelector('form')!);
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		await waitFor(() => expect(editor.textContent).toBe(''));
		return { container, editor };
	}
	async function startOnNewChat() {
		const { container, editor } = await sendOnNewChat();
		let finish!: (value: unknown) => void;
		const upload = vi.spyOn(api, 'uploadFile').mockImplementation(() => new Promise(done => { finish = done; }) as never);
		return { upload, container, editor, finish: (v: unknown) => finish(v) };
	}

	it('an attachment finishing after the New chat took its id lands in that chat\'s editor', async () => {
		const { upload, container, editor, finish } = await startOnNewChat();
		await pickFile(container);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		expect(upload.mock.calls[0][0]).toBe('/projects/app/upload');
		adopt('made');
		await new Promise(r => setTimeout(r, 20));
		finish(uploaded);
		await waitFor(() => expect(editor.innerHTML).toContain('uploads/a.txt'));
		expect(storedDraft(null)).not.toContain('uploads/a.txt');
	});

	it('a large paste finishing after the New chat took its id lands in that chat\'s editor', async () => {
		const { upload, editor, finish } = await startOnNewChat();
		paste(editor, 'q'.repeat(LARGE_PASTE_BYTES));
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		adopt('made');
		await new Promise(r => setTimeout(r, 20));
		finish({ ...uploaded, path: 'uploads/pasted.txt', filename: 'pasted.txt' });
		await waitFor(() => expect(editor.innerHTML).toContain('uploads/pasted.txt'));
		expect(storedDraft(null)).not.toContain('pasted.txt');
	});

	it('taken its id and then left: the result goes to the created chat\'s draft, not the open chat', async () => {
		const { upload, container, editor, finish } = await startOnNewChat();
		await pickFile(container);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		adopt('made');
		await new Promise(r => setTimeout(r, 20));
		navigateTo('chat-b');
		await new Promise(r => setTimeout(r, 20));
		finish(uploaded);
		await waitFor(() => expect(storedDraft('made')).toContain('uploads/a.txt'));
		expect(editor.innerHTML).not.toContain('uploads/a.txt');
		expect(storedDraft('chat-b')).not.toContain('uploads/a.txt');
		expect(storedDraft(null)).not.toContain('uploads/a.txt');
	});

	it('two attachments on one New chat, one finished before it took its id and one after, end in the same chat', async () => {
		const { container, editor } = await sendOnNewChat();
		const finishes: Array<(value: unknown) => void> = [];
		const upload = vi.spyOn(api, 'uploadFile').mockImplementation(() => new Promise(done => { finishes.push(done); }) as never);
		const input = container.querySelector('input[type="file"]') as HTMLInputElement;
		Object.defineProperty(input, 'files', { configurable: true, value: [new File(['x'], 'a.txt'), new File(['y'], 'b.txt')] });
		await fireEvent.change(input);
		await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
		finishes[0](uploaded);
		await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
		adopt('made');
		await new Promise(r => setTimeout(r, 20));
		finishes[1]({ ...uploaded, path: 'uploads/b.txt', filename: 'b.txt' });
		await waitFor(() => expect(editor.innerHTML).toContain('uploads/b.txt'));
		expect(editor.innerHTML).toContain('uploads/a.txt');
		expect(storedDraft(null)).not.toContain('uploads/');
	});

	it('left before it took an id: the result goes back to the New chat slot, not the open chat', async () => {
		const { upload, container, editor, finish } = await startOnNewChat();
		await pickFile(container);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		navigateTo('chat-b');
		await new Promise(r => setTimeout(r, 20));
		finish(uploaded);
		await waitFor(() => expect(storedDraft(null)).toContain('uploads/a.txt'));
		expect(editor.innerHTML).not.toContain('uploads/a.txt');
		expect(storedDraft('chat-b')).not.toContain('uploads/a.txt');
	});
});

// #1659 S6. A chat another worker runs with no shared id: no session upload
// exists for it and its id may name an unrelated project here. The attach
// control says so before a file is picked, and nothing is uploaded.
describe('a chat another worker runs whose project is not linked here (#1659)', () => {
	it('refuses attaching with a way forward, and uploads nothing', async () => {
		Object.assign(activeSession.current, { projectV2Id: null, isRemote: true });
		const upload = vi.spyOn(api, 'uploadFile').mockResolvedValue(uploaded as never);
		const { container } = render(ChatInput);
		const attach = container.querySelector('button.composer-attach') as HTMLButtonElement;
		expect(attach.title).toBe(ATTACHMENTS_REMOTE_UNLINKED);
		await fireEvent.click(attach);
		await pickFile(container);
		await new Promise(r => setTimeout(r, 50));
		expect(upload).not.toHaveBeenCalled();
		// It becomes attachable without a reload once a row links the project.
		activeSession.reconcileOwner([{ ...row(true), project_v2_id: 'pv2-linked' }]);
		await waitFor(() => expect(attach.title).toBe('Attach files'));
		await pickFile(container);
		await waitFor(() => expect(upload).toHaveBeenCalledOnce());
		expect(upload.mock.calls[0][0]).toBe('/sessions/owned/upload');
	});
});

