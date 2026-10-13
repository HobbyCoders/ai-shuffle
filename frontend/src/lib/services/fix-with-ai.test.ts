/**
 * Tests for fix-with-ai.ts — prompt building + review-first chat start.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	newChat: vi.fn(),
	setDraft: vi.fn(),
	sendMessage: vi.fn(),
	openChat: vi.fn(),
	setSidebarCollapsed: vi.fn(),
	layoutCurrent: { value: { sidebarOverlay: false, sidebarCollapsed: false } },
}));

vi.mock('$lib/stores/active-session', () => ({
	activeSession: {
		newChat: mocks.newChat,
		setDraft: mocks.setDraft,
		sendMessage: mocks.sendMessage,
	},
}));
vi.mock('$lib/stores/layout.svelte', () => ({
	layout: {
		get current() {
			return mocks.layoutCurrent.value;
		},
		setSidebarCollapsed: mocks.setSidebarCollapsed,
		openChat: mocks.openChat,
	},
}));

import { buildFixPrompt, startFixChat } from './fix-with-ai';

beforeEach(() => {
	Object.values(mocks).forEach((m) => {
		if (typeof (m as { mockReset?: () => void }).mockReset === 'function') {
			(m as unknown as { mockReset: () => void }).mockReset();
		}
	});
	mocks.layoutCurrent.value = { sidebarOverlay: false, sidebarCollapsed: false };
});

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
		expect(mocks.openChat).toHaveBeenCalledWith(null);
		// Review-first: must NOT auto-send.
		expect(mocks.sendMessage).not.toHaveBeenCalled();
	});

	it('collapses an overlay sidebar before opening', () => {
		mocks.layoutCurrent.value = { sidebarOverlay: true, sidebarCollapsed: false };
		const rafSpy = vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => {
			(cb as FrameRequestCallback)(0);
			return 0;
		});

		startFixChat({ message: 'boom' });

		expect(mocks.setSidebarCollapsed).toHaveBeenCalledWith(true);
		expect(mocks.openChat).toHaveBeenCalledWith(null);
		rafSpy.mockRestore();
	});
});
