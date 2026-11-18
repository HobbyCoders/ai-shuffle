/**
 * Tests for fix-with-ai.ts — prompt building + review-first chat start.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	newChat: vi.fn(),
	setDraft: vi.fn(),
	sendMessage: vi.fn(),
}));

vi.mock('$lib/stores/active-session', () => ({
	activeSession: {
		newChat: mocks.newChat,
		setDraft: mocks.setDraft,
		sendMessage: mocks.sendMessage,
	},
}));

import { buildFixPrompt, startFixChat } from './fix-with-ai';
import { layout } from '$lib/stores/layout.svelte';
import { shellHistory } from './app-history.svelte';

const openChat = () => vi.mocked(layout.openChat);

beforeEach(() => {
	Object.values(mocks).forEach((m) => {
		if (typeof (m as { mockReset?: () => void }).mockReset === 'function') {
			(m as unknown as { mockReset: () => void }).mockReset();
		}
	});
	layout.setSidebarOverlay(false);
	layout.setSidebarCollapsed(false);
	vi.spyOn(layout, 'openChat');
});
afterEach(() => { shellHistory.stop(); vi.restoreAllMocks(); });

describe('buildFixPrompt', () => {
	it('instructs the agent to use GetAppErrors', () => {
		const prompt = buildFixPrompt({ message: 'boom' });
		expect(prompt).toContain('GetAppErrors');
		expect(prompt).toContain('boom');
	});

	it('includes source, status, and url when present', () => {
		const prompt = buildFixPrompt({
			message: 'boom',
			source: 'ws',
			status_code: 500,
			url: 'https://app/x',
		});
		expect(prompt).toContain('source: ws');
		expect(prompt).toContain('status: 500');
		expect(prompt).toContain('url: https://app/x');
	});

	it('puts detail in a fenced block', () => {
		const prompt = buildFixPrompt({ message: 'boom', detail: 'STACK_TRACE_LINE' });
		expect(prompt).toContain('```');
		expect(prompt).toContain('STACK_TRACE_LINE');
	});

	it('omits optional fields cleanly when absent', () => {
		const prompt = buildFixPrompt({ message: 'boom' });
		expect(prompt).not.toContain('status:');
		expect(prompt).not.toContain('url:');
	});
});

describe('startFixChat', () => {
	it('opens a new chat and pre-fills the draft WITHOUT sending', () => {
		startFixChat({ message: 'boom', source: 'ws' });

		expect(mocks.newChat).toHaveBeenCalledTimes(1);
		expect(mocks.setDraft).toHaveBeenCalledTimes(1);
		expect(mocks.setDraft.mock.calls[0][0]).toContain('GetAppErrors');
		expect(openChat()).toHaveBeenCalledWith(null);
		// Review-first: must NOT auto-send.
		expect(mocks.sendMessage).not.toHaveBeenCalled();
	});

	it('collapses an overlay sidebar before opening', () => {
		layout.setSidebarOverlay(true);
		layout.setSidebarCollapsed(false);
		const rafSpy = vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => {
			(cb as FrameRequestCallback)(0);
			return 0;
		});

		startFixChat({ message: 'boom' });

		expect(layout.current.sidebarCollapsed).toBe(true);
		expect(openChat()).toHaveBeenCalledWith(null);
		rafSpy.mockRestore();
	});

	it('a Back or Forward before the next frame wins over the deferred New Chat (S18)', () => {
		shellHistory.start({ shell: { id: 'notes', place: { type: 'notes' } } }, { type: 'notes' });
		layout.setSidebarOverlay(true);
		layout.setSidebarCollapsed(false);
		const frames: FrameRequestCallback[] = [];
		const rafSpy = vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => frames.push(cb));

		startFixChat({ message: 'boom' });
		expect(shellHistory.land({ shell: { id: 'docs', place: { type: 'docs' } } })).toBe(true);
		for (const frame of frames.splice(0)) frame(0);

		expect(openChat()).not.toHaveBeenCalled();
		rafSpy.mockRestore();
	});
});
