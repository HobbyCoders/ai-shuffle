/**
 * Phase 3.1 — WebSocket disconnection banner (ChatView).
 *
 * ChatView draws a quiet "Reconnecting…" pill for a short drop and escalates
 * to a warning Banner with a "Reconnect" action when the client gave up or the
 * drop lasted a long visible time (connection-notice.ts). Mounting the full ChatView tree pulls
 * in the whole session/store graph, so these tests render ConnectionNotice —
 * the component ChatView draws both surfaces through — with the props ChatView
 * passes, plus the tier and copy rules and the visibility predicate. A manual
 * tap shows busy at once, ignores repeat taps, and reports a failure.
 */
import { describe, it, expect, vi, afterEach, beforeAll, afterAll } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import ConnectionNotice from './ConnectionNotice.svelte';
import { connectionNoticeCopy, connectionNoticeTier, RECONNECT_BANNER_ESCALATE_MS } from './connection-notice';
import { DEFAULT_CONNECTION_BANNER_GRACE_MS } from '$lib/stores/session/connection-banner-grace';

beforeAll(() => {
	// The pill reports its height (bind:offsetHeight) through ResizeObserver.
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});
afterAll(() => {
	vi.unstubAllGlobals();
});
afterEach(() => {
	cleanup();
});

// ChatView's eligibility: an active session whose socket is down.
//   sessionId && !wsConnected
function shouldShowConnectionBanner(s: {
	sessionId: string | null;
	wsConnected: boolean;
}): boolean {
	return !!s.sessionId && !s.wsConnected;
}

describe('ChatView connection-banner gate', () => {
	it('shows when a live session is disconnected', () => {
		expect(
			shouldShowConnectionBanner({ sessionId: 'sess-1', wsConnected: false })
		).toBe(true);
	});

	it('hides while connected', () => {
		expect(
			shouldShowConnectionBanner({ sessionId: 'sess-1', wsConnected: true })
		).toBe(false);
	});

	it('hides when there is no active session (empty / new chat)', () => {
		expect(
			shouldShowConnectionBanner({ sessionId: null, wsConnected: false })
		).toBe(false);
	});
});

describe('ChatView connection notice tiers', () => {
	const tier = (noticeDue: boolean, bannerDue: boolean, reconnectRequired: boolean) =>
		connectionNoticeTier({ noticeDue, bannerDue, reconnectRequired });

	it('draws nothing inside the short visible grace, even for a given-up client', () => {
		expect(tier(false, false, false)).toBe('none');
		expect(tier(false, false, true)).toBe('none');
	});

	it('draws the quiet pill for a short drop', () => {
		expect(tier(true, false, false)).toBe('pill');
	});

	it('escalates to the banner after a long visible drop or once the client gave up', () => {
		expect(tier(true, true, false)).toBe('banner');
		expect(tier(true, false, true)).toBe('banner');
	});

	it('escalates well after the short grace', () => {
		expect(RECONNECT_BANNER_ESCALATE_MS).toBeGreaterThan(DEFAULT_CONNECTION_BANNER_GRACE_MS * 3);
	});
});

describe('ChatView connection notice after a manual tap', () => {
	it('keeps the tier while a tap runs and escalates a failed tap to the banner', () => {
		const t = (manual: 'idle' | 'connecting' | 'failed', bannerDue = false) =>
			connectionNoticeTier({ noticeDue: true, bannerDue, reconnectRequired: false, manual });
		expect(t('connecting')).toBe('pill');
		expect(t('connecting', true)).toBe('banner');
		expect(t('failed')).toBe('banner');
		// Still nothing inside the short grace.
		expect(connectionNoticeTier({ noticeDue: false, bannerDue: false, reconnectRequired: false, manual: 'failed' })).toBe('none');
	});

	it('says who it is connecting to and what happened', () => {
		const busy = connectionNoticeCopy({ reconnectRequired: true, manual: 'connecting', workerName: 'Unraid' });
		expect(busy).toMatchObject({ busy: true, actionLabel: 'Reconnecting…', pillLabel: 'Connecting to Unraid…', pillHint: '' });
		expect(busy.message).toMatch(/^Connecting to Unraid…/);
		const failed = connectionNoticeCopy({ reconnectRequired: false, manual: 'failed', workerName: 'Unraid' });
		expect(failed).toMatchObject({ busy: false, actionLabel: 'Try again' });
		expect(failed.message).toBe("Couldn't reach Unraid. Still retrying in the background.");
		expect(connectionNoticeCopy({ reconnectRequired: true, manual: 'failed', workerName: null }).message)
			.toBe("Couldn't reach your worker. Try again in a moment.");
		expect(connectionNoticeCopy({ reconnectRequired: true, manual: 'idle' }).message)
			.toBe('Chat controls disconnected. Reconnect to send messages.');
	});
});

describe('ChatView connection notice render + reconnect', () => {
	function renderNotice(props: Partial<{
		tier: 'none' | 'pill' | 'banner';
		reconnectRequired: boolean;
		manual: 'idle' | 'connecting' | 'failed';
		workerName: string | null;
	}>, onReconnect = vi.fn()) {
		const view = render(ConnectionNotice, {
			props: { tier: 'banner', reconnectRequired: false, manual: 'idle', workerName: 'Unraid', onReconnect, ...props }
		});
		return { ...view, onReconnect };
	}

	it('renders the disconnected banner and fires the reconnect handler', async () => {
		const { onReconnect } = renderNotice({});
		expect(screen.getByText(/chat controls disconnected/i)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
		expect(onReconnect).toHaveBeenCalledOnce();
	});

	it('shows the banner button busy and ignores taps while the handshake runs', async () => {
		const { onReconnect } = renderNotice({ manual: 'connecting' });
		const button = screen.getByRole('button', { name: 'Reconnecting…' });
		expect(button.getAttribute('aria-busy')).toBe('true');
		expect(button.getAttribute('aria-disabled')).toBe('true');
		// Still focusable: not the native disabled attribute, which drops focus.
		expect((button as HTMLButtonElement).disabled).toBe(false);
		expect(button.querySelector('svg')).toBeTruthy();
		expect(screen.getByText(/Connecting to Unraid…/)).toBeTruthy();
		await fireEvent.click(button);
		await fireEvent.click(button);
		expect(onReconnect).not.toHaveBeenCalled();
	});

	it('reports a failed tap in plain words and offers another try', async () => {
		const { onReconnect, container } = renderNotice({ manual: 'failed' });
		expect(screen.getByText("Couldn't reach Unraid. Still retrying in the background.")).toBeTruthy();
		expect(container.querySelector('[data-manual-reconnect="failed"]')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		expect(onReconnect).toHaveBeenCalledOnce();
	});

	it('announces the pill state change in its live region and retries from the pill', async () => {
		const { onReconnect, rerender } = renderNotice({ tier: 'pill' });
		const pill = screen.getByRole('button', { name: /Reconnecting…/ });
		expect(pill.getAttribute('aria-busy')).toBeNull();
		await fireEvent.click(pill);
		expect(onReconnect).toHaveBeenCalledOnce();
		await rerender({ tier: 'pill', manual: 'connecting' });
		const busy = screen.getByRole('button', { name: 'Connecting to Unraid…' });
		expect(busy.getAttribute('aria-busy')).toBe('true');
		expect(busy.closest('[role="status"]')?.getAttribute('aria-live')).toBe('polite');
		await fireEvent.click(busy);
		expect(onReconnect).toHaveBeenCalledOnce();
	});

	it('draws nothing tappable at the none tier but keeps the live region mounted', () => {
		renderNotice({ tier: 'none' });
		expect(screen.queryByRole('button')).toBeNull();
		expect(screen.getByRole('status')).toBeTruthy();
	});
});
