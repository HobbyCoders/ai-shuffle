import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import { activeSession } from '$lib/stores/active-session';
import { appMode, type SessionMode } from '$lib/stores/sessionMode.svelte';
import ControlsDock from './ControlsDock.svelte';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
		ok: false,
		status: 404,
		json: vi.fn().mockResolvedValue({})
	}));
});

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return {
		...actual,
		getUsageLimits: vi.fn().mockResolvedValue({
			available: false,
			buckets: {},
			provider_buckets: {}
		})
	};
});

// The dock reads its lane off `$activeSession.mode`, and `newChat()` is what
// stamps the sidepanel's lane onto a fresh compose surface — so it is also how
// a test puts the dock in one. Every test starts (and leaves) the code lane.
function setLane(mode: SessionMode) {
	appMode.set(mode);
	activeSession.newChat();
}

beforeEach(() => setLane('code'));
afterEach(() => {
	cleanup();
	setLane('code');
});
afterAll(() => vi.unstubAllGlobals());

function renderDock(props: Record<string, unknown> = {}) {
	return render(ControlsDock, {
		props: {
			isUploading: false,
			onAttachClick: vi.fn(),
			...props
		}
	});
}

const voiceActions = createRawSnippet(() => ({
	render: () => `<span data-testid="voice-snippet">voice</span>`
}));

describe('ControlsDock split controls', () => {
	it('splits the old summary chip into permission, model and effort controls', () => {
		const { container } = renderDock();

		expect(container.querySelector('.controls-summary-chip')).toBeNull();
		expect(screen.getByRole('button', { name: /^permission mode:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^model:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^effort:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /attach file/i })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /more input options/i })).toBeNull();
	});

	it('orders the row permission · attach on the left, model · effort · ring on the right', () => {
		const { container } = renderDock();

		const left = container.querySelector('.dock-left');
		const right = container.querySelector('.dock-right');
		const permission = screen.getByRole('button', { name: /^permission mode:/i });
		const attach = screen.getByRole('button', { name: /attach file/i });
		expect(left?.contains(permission)).toBe(true);
		expect(left?.contains(attach)).toBe(true);
		expect(permission.compareDocumentPosition(attach) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(right?.contains(screen.getByRole('button', { name: /^model:/i }))).toBe(true);
		expect(right?.contains(screen.getByRole('button', { name: /^effort:/i }))).toBe(true);
		expect(right?.querySelector('.usage-context-wrapper')).toBeTruthy();
		expect(left && right && (left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING))
			.toBeTruthy();
	});

	it('places attach after the voice snippet in the left cluster', () => {
		const { container } = renderDock({ voiceActions });

		const left = container.querySelector('.dock-left');
		const voice = left?.querySelector('[data-testid="voice-snippet"]');
		const attach = screen.getByRole('button', { name: /attach file/i });
		expect(voice).toBeInstanceOf(Element);
		expect(left?.contains(attach)).toBe(true);
		if (!(voice instanceof Element)) return;
		expect(voice.compareDocumentPosition(attach) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
	});

	it('opens the compact effort popover from the effort tag', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^effort:/i }));

		const dialog = screen.getByRole('dialog', { name: /reasoning effort/i });
		expect(dialog.querySelector('.effort-pop-card')).toBeTruthy();
		expect(screen.getByRole('slider', { name: /reasoning effort/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^effort:/i }).getAttribute('aria-expanded')).toBe('true');
	});

	it('opens the tool-access menu from the permission text', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^permission mode:/i }));

		const menu = screen.getByRole('menu', { name: /tool access/i });
		expect(menu.querySelectorAll('[role="menuitemradio"]')).toHaveLength(5);
		expect(screen.getByRole('menuitemradio', { name: /bypass/i })).toBeTruthy();
	});

	it('keeps only one dock popover open at a time and closes on Escape', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^effort:/i }));
		expect(screen.getByRole('dialog', { name: /reasoning effort/i })).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: /^permission mode:/i }));
		expect(screen.queryByRole('dialog', { name: /reasoning effort/i })).toBeNull();
		expect(screen.getByRole('menu', { name: /tool access/i })).toBeTruthy();

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('menu', { name: /tool access/i })).toBeNull();
	});

	it('opens the model picker from the model text', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^model:/i }));

		const dialog = screen.getByRole('dialog', { name: /choose model/i });
		expect(dialog.querySelector('[data-model-picker-panel]')).toBeTruthy();
	});

	it('drops the tool-access control in the chat lane but keeps attach', () => {
		setLane('chat');
		const { container } = renderDock({ voiceActions });

		// A chat session's tool surface is fixed server-side, so there is nothing
		// for a permission menu to grant.
		expect(screen.queryByRole('button', { name: /^permission mode:/i })).toBeNull();
		// Attach survives: chat uploads land in the workspace's shared
		// chat-uploads dir, and the chat ceiling carries a Read scoped to it.
		const attach = screen.getByRole('button', { name: /attach file/i });
		expect(attach).toBeTruthy();
		// Not captioned with the project advice — a projectless chat is the
		// working state here, not a misconfiguration. (Whether the button is
		// enabled depends on credentials, which this harness has none of.)
		expect(attach.getAttribute('title')).toBe('Attach file');
		// Everything else the chat composer keeps.
		expect(screen.getByRole('button', { name: /^model:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^effort:/i })).toBeTruthy();
		expect(container.querySelector('[data-testid="voice-snippet"]')).toBeTruthy();
		expect(container.querySelector('.usage-context-wrapper')).toBeTruthy();
		expect(container.querySelector('.mobile-settings-trigger')).toBeTruthy();
	});

	it('drops attach in the voice lane, which has no composer upload path', () => {
		setLane('voice');
		renderDock({ voiceActions });

		expect(screen.queryByRole('button', { name: /attach file/i })).toBeNull();
	});

	it('does not wedge the dock when the lane flips with the access menu open', async () => {
		renderDock();
		await fireEvent.click(screen.getByRole('button', { name: /^permission mode:/i }));
		expect(screen.getByRole('menu', { name: /tool access/i })).toBeTruthy();

		setLane('chat');
		await tick();

		expect(screen.queryByRole('menu', { name: /tool access/i })).toBeNull();
		// The stranded popover would otherwise keep `openPopover` pinned to a
		// trigger that no longer exists, and no outside click could clear it.
		await fireEvent.click(screen.getByRole('button', { name: /^effort:/i }));
		expect(screen.getByRole('dialog', { name: /reasoning effort/i })).toBeTruthy();
	});
});
