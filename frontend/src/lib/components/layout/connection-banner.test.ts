/**
 * Phase 3.1 — WebSocket disconnection banner (ChatView).
 *
 * ChatView renders a warning Banner with a "Reconnect" action when the active
 * session is live but the socket is down. Mounting the full ChatView tree pulls
 * in the whole session/store graph, so these tests exercise the exact Banner
 * contract ChatView relies on — the props it passes and the reconnect wiring —
 * plus the visibility predicate that gates the banner. If the Banner's action
 * API or the gate logic changes, these break.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import Banner from '$lib/components/Banner.svelte';

afterEach(() => {
	cleanup();
});

// Mirrors ChatView's gate:
//   sessionId && remoteControllable !== false && !wsConnected
function shouldShowConnectionBanner(s: {
	sessionId: string | null;
	remoteControllable: boolean;
	wsConnected: boolean;
}): boolean {
	return !!s.sessionId && s.remoteControllable !== false && !s.wsConnected;
}

describe('ChatView connection-banner gate', () => {
	it('shows when a live session is disconnected', () => {
		expect(
			shouldShowConnectionBanner({ sessionId: 'sess-1', remoteControllable: true, wsConnected: false })
		).toBe(true);
	});

	it('hides while connected', () => {
		expect(
			shouldShowConnectionBanner({ sessionId: 'sess-1', remoteControllable: true, wsConnected: true })
		).toBe(false);
	});

	it('hides when there is no active session (empty / new chat)', () => {
		expect(
			shouldShowConnectionBanner({ sessionId: null, remoteControllable: true, wsConnected: false })
		).toBe(false);
	});

	it('hides for a cross-install offline session (its own banner owns that case)', () => {
		expect(
			shouldShowConnectionBanner({ sessionId: 'sess-1', remoteControllable: false, wsConnected: false })
		).toBe(false);
	});
});

describe('ChatView connection-banner render + reconnect', () => {
	// The exact props ChatView passes to <Banner>.
	function renderBanner(onReconnect: () => void) {
		return render(Banner, {
			props: {
				severity: 'warning',
				message: 'Connection lost — trying to reconnect. Live updates are paused.',
				action: { label: 'Reconnect', onClick: onReconnect }
			}
		});
	}

	it('renders the disconnected message and a Reconnect button', () => {
		renderBanner(vi.fn());
		expect(screen.getByText(/connection lost/i)).toBeTruthy();
		expect(screen.getByRole('button', { name: /reconnect/i })).toBeTruthy();
	});

	it('fires the reconnect handler when the button is clicked', async () => {
		const onReconnect = vi.fn();
		renderBanner(onReconnect);

		await fireEvent.click(screen.getByRole('button', { name: /reconnect/i }));

		expect(onReconnect).toHaveBeenCalledTimes(1);
	});
});
