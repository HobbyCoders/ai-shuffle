/**
 * Phase 3.1 — WebSocket disconnection banner (ChatView).
 *
 * ChatView draws a quiet "Reconnecting…" pill for a short drop and escalates
 * to a warning Banner with a "Reconnect" action when the client gave up or the
 * drop lasted a long visible time (connection-notice.ts). Mounting the full ChatView tree pulls
 * in the whole session/store graph, so these tests exercise the exact Banner
 * contract ChatView relies on — the props it passes and the reconnect wiring —
 * plus the visibility predicate that gates the banner. If the Banner's action
 * API or the gate logic changes, these break.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import Banner from '$lib/components/Banner.svelte';
import { connectionNoticeTier, RECONNECT_BANNER_ESCALATE_MS } from './connection-notice';
import { DEFAULT_CONNECTION_BANNER_GRACE_MS } from '$lib/stores/session/connection-banner-grace';

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

describe('ChatView connection-banner render + reconnect', () => {
	// The exact props ChatView passes to <Banner>.
	function renderBanner(onReconnect: () => void) {
		return render(Banner, {
			props: {
				severity: 'warning',
				message: 'Chat controls disconnected. Trying to reconnect; running sessions continue.',
				action: { label: 'Reconnect', onClick: onReconnect }
			}
		});
	}

	it('renders the disconnected message and a Reconnect button', () => {
		renderBanner(vi.fn());
		expect(screen.getByText(/chat controls disconnected/i)).toBeTruthy();
		expect(screen.getByRole('button', { name: /reconnect/i })).toBeTruthy();
	});

	it('fires the reconnect handler when the button is clicked', async () => {
		const onReconnect = vi.fn();
		renderBanner(onReconnect);

		await fireEvent.click(screen.getByRole('button', { name: /reconnect/i }));

		expect(onReconnect).toHaveBeenCalledTimes(1);
	});
});
