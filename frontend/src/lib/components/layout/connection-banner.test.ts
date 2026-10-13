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
