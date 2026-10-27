import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ChatInput from './ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import '$lib/test-stubs/composer-dom';

// #1831: before a send into a large chat whose provider cache went cold, the
// composer says what it costs and offers "Compact first" beside "Send anyway".
const calls: string[] = [];
let cache: Record<string, unknown> = {};

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		calls.push(`${init?.method ?? 'GET'} ${url}`);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/cache-state')
				? cache
				: url.includes('/compact')
					? { turn_id: 't-compact', session_id: 'cold' }
					: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

const TEXT = 'carry on';
const COLD = {
	leg: 'delegated', context_tokens: 412_000, last_call_at: 1, idle_seconds: 5400,
	cache_ttl_seconds: 3600, cache_write_ttl: '1h', threshold_tokens: 150_000, cold: true, large: true, warn: true
};
let sends: MockInstance;

beforeEach(async () => {
	calls.length = 0;
	localStorage.clear();
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, {
		sessionId: 'cold', mode: 'chat', profile: 'coding', isStreaming: false, liveTurnId: null,
		error: null, worktreeError: null
	});
	activeSession.setDraft(TEXT);
	sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	activeSession.setDraft('');
	vi.restoreAllMocks();
});

async function submit(withAttachment = false) {
	const view = render(ChatInput);
	const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
	await waitFor(() => withAttachment
		? expect(editor.querySelector('[data-file-chip]')).toBeTruthy()
		: expect(editor.textContent).toBe(TEXT));
	await fireEvent.submit(view.container.querySelector('form')!);
	return { ...view, editor };
}

describe('cold send warning', () => {
	it.each(['see @/uploads/proof.png please', ''])('restores and sends attachments exactly once: %s', async (prompt) => {
		cache = { ...COLD, cold:false, warn:false };
		activeSession.setDraft('');
		const view = render(ChatInput);
		activeSession.setPendingRestorePrompt(prompt, ['/uploads/proof.png']);
		await waitFor(() => expect(view.container.querySelectorAll('[data-file-chip]')).toHaveLength(1));
		expect(sends).not.toHaveBeenCalled();
		await fireEvent.submit(view.container.querySelector('form')!);
		await waitFor(() => expect(sends).toHaveBeenCalledExactlyOnceWith(prompt || '@/uploads/proof.png', {
			pendingUploadPaths:['/uploads/proof.png']
		}));
	});
	it('restores unsent words and file chips to the composer without sending', async () => {
		activeSession.setDraft('');
		const view = render(ChatInput);
		activeSession.setPendingRestorePrompt('original words', ['/uploads/proof.png']);
		const editor = view.container.querySelector<HTMLElement>('.tiptap')!;
		await waitFor(() => expect(editor.textContent).toContain('original words'));
		await waitFor(() => expect(editor.querySelector('[data-file-chip]')).toBeTruthy());
		expect(editor.textContent).toContain('proof.png');
		expect(activeSession.current.pendingRestorePrompt).toBeNull();
		expect(activeSession.current.pendingRestoreUploadPaths).toBeUndefined();
		expect(sends).not.toHaveBeenCalled();
	});
	it.each(['1h', '5m'] as const)('asks before a large cold send with %s writes, and remembers Send anyway', async (writeTtl) => {
		cache = { ...COLD, cache_write_ttl: writeTtl, cost_comparison: {
			model: 'claude-sonnet-5', cold_resume_usd: 1.69, compaction_usd: 1.73,
			continued_usd: 0.37, compact_continue_usd: 2.1,
			compacted_context_tokens: 82_400, reply_tokens: 4_000, summary_tokens: 8_000,
			recommendation: 'send'
		} };
		const { editor } = await submit();
		await waitFor(() => expect(screen.getByText(/holds about 412k tokens/)).toBeTruthy());
		expect(screen.getByText(/cache has likely expired/)).toBeTruthy();
		expect(screen.getByText(new RegExp(`Includes ${writeTtl === '5m' ? '5-minute' : '1-hour'} cache writes`))).toBeTruthy();
		expect(screen.getByText(/Cold resume total: ~\$1.69/)).toBeTruthy();
		expect(screen.getByText(/Compact and continue total: ~\$2.10/)).toBeTruthy();
		expect(screen.getByText(/Recommendation: Send anyway for this one call/)).toBeTruthy();
		expect(sends).not.toHaveBeenCalled();
		expect(editor.textContent).toBe(TEXT);
		await fireEvent.click(screen.getByRole('button', { name: 'Send anyway' }));
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(sends.mock.calls[0][0]).toBe(TEXT);
		// The same chat at the same size is not asked again.
		cleanup();
		activeSession.setDraft(TEXT);
		await submit();
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(2));
		expect(screen.queryByRole('button', { name: 'Send anyway' })).toBeNull();
	});

	it('Compact first submits the message for server-owned compaction and clears the composer', async () => {
		cache = COLD;
		const { editor } = await submit();
		await waitFor(() => expect(screen.getByRole('button', { name: 'Compact first' })).toBeTruthy());
		await fireEvent.click(screen.getByRole('button', { name: 'Compact first' }));
		await waitFor(() => expect(sends).toHaveBeenCalledExactlyOnceWith(TEXT, {
			compactFirst: true, pendingUploadPaths: undefined
		}));
		await waitFor(() => expect(screen.queryByRole('button', { name: 'Compact first' })).toBeNull());
		expect(editor.textContent).toBe('');
		expect(activeSession.current.draft).toBe('');
	});

	it('includes inline attachment paths in compact-and-send and clears the attachment chip', async () => {
		cache = COLD;
		activeSession.setDraft(JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [
			{ type: 'text', text: TEXT + ' ' },
			{ type: 'fileChip', attrs: { path: 'notes.txt', fullPath: '/uploads/notes.txt', filename: 'notes.txt', type: 'file' } }
		] }] }));
		const { editor } = await submit(true);
		await fireEvent.click(await screen.findByRole('button', { name: 'Compact first' }));
		await waitFor(() => expect(sends).toHaveBeenCalledExactlyOnceWith(TEXT + ' @/uploads/notes.txt', {
			compactFirst: true, pendingUploadPaths: ['/uploads/notes.txt']
		}));
		expect(editor.textContent).toBe('');
	});

	it('keeps a failed compact-and-send visible and allows retry', async () => {
		cache = COLD;
		sends.mockResolvedValueOnce(false);
		const { editor } = await submit();
		await fireEvent.click(await screen.findByRole('button', { name: 'Compact first' }));
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		await waitFor(() => expect(editor.textContent).toBe(TEXT));
		expect(screen.getByRole('button', { name: 'Compact first' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Compact first' }));
		await waitFor(() => expect(sends).toHaveBeenCalledTimes(2));
		await waitFor(() => expect(editor.textContent).toBe(''));
	});

	it('does not send twice while compact-and-send is being accepted', async () => {
		cache = COLD;
		let accept!: (accepted: boolean) => void;
		sends.mockImplementationOnce(() => new Promise<boolean>((resolve) => { accept = resolve; }));
		const { editor, container } = await submit();
		await fireEvent.click(await screen.findByRole('button', { name: 'Compact first' }));
		await waitFor(() => expect(screen.getByText('Starting compaction. Queuing your message to send next…')).toBeTruthy());
		expect(editor.textContent).toBe('');
		expect(screen.queryByRole('button', { name: 'Send anyway' })).toBeNull();
		expect(screen.getByRole('button', { name: 'Send' }).hasAttribute('disabled')).toBe(true);
		await fireEvent.submit(container.querySelector('form')!);
		expect(sends).toHaveBeenCalledOnce();
		accept(true);
		await waitFor(() => expect(screen.queryByText('Starting compaction. Queuing your message to send next…')).toBeNull());
	});

	it('sends at once when the cache is warm or the chat is small', async () => {
		cache = { ...COLD, warn: false, cold: false };
		await submit();
		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		expect(screen.queryByRole('button', { name: 'Send anyway' })).toBeNull();
	});
});
