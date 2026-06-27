/**
 * Tests for the unified notification store — the backward-compatible `toast`
 * alias mapping and the severity-based persist defaults that decide what lands
 * in the bell (history) vs. what stays live-only.
 *
 * Network (`$lib/api/client`) and auth are mocked so the store stays pure: with
 * auth reported unauthenticated, the store's remote-persist guards short-circuit
 * and no POST fires.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/api/client', () => ({
	api: {
		get: vi.fn(() => Promise.resolve({})),
		post: vi.fn(() => Promise.resolve({})),
		delete: vi.fn(() => Promise.resolve({})),
	},
}));

// Unauthenticated → `_persistRemote` / `_remote` early-return, so no network.
vi.mock('./auth.svelte', () => ({
	auth: { current: { status: { authenticated: false } } },
}));

import { notify, toast, notifications, notificationHistory, unreadCount } from './notifications';

/** Wipe both slices between tests so counts/ids don't leak across cases. */
function resetStore() {
	for (const n of [...notifications.current.live, ...notifications.current.history]) {
		notify.dismiss(n.id);
	}
}

describe('notifications store', () => {
	beforeEach(() => {
		resetStore();
		vi.clearAllMocks();
	});

	describe('toast alias (legacy facade)', () => {
		it('maps tone → severity, defaults to a transient (non-persisted) toast', () => {
			toast.push('Saved', { tone: 'success' });

			const { live, history } = notifications.current;
			expect(live).toHaveLength(1);
			expect(live[0].severity).toBe('success');
			expect(live[0].surface).toBe('toast');
			expect(live[0].persist).toBe(false);
			// Legacy toasts are ephemeral — they never reach the bell.
			expect(history).toHaveLength(0);
		});

		it('returns a numeric id that dismiss() accepts', () => {
			const id = toast.push('Hello');
			expect(typeof id).toBe('number');
			expect(notifications.current.live).toHaveLength(1);

			toast.dismiss(id);
			expect(notifications.current.live).toHaveLength(0);
		});

		it('defaults an untoned toast to info severity', () => {
			toast.push('Heads up');
			expect(notifications.current.live[0].severity).toBe('info');
		});
	});

	describe('persist defaults by severity', () => {
		it('persists errors and warnings to history (the bell)', () => {
			notify.error('Boom');
			notify.warning('Careful');

			expect(notificationHistory.current).toHaveLength(2);
			expect(notificationHistory.current.every((n) => n.persist)).toBe(true);
		});

		it('keeps success and info live-only (not in history)', () => {
			notify.success('Nice');
			notify.info('FYI');

			expect(notifications.current.live).toHaveLength(2);
			expect(notificationHistory.current).toHaveLength(0);
		});

		it('honors an explicit persist override against the severity default', () => {
			notify.success('Important success', { persist: true });
			notify.error('Transient error', { persist: false });

			const titles = notificationHistory.current.map((n) => n.message);
			expect(titles).toContain('Important success');
			expect(titles).not.toContain('Transient error');
		});
	});

	describe('ingest (server-originated)', () => {
		it('adds a server notification to history as read-state false, source server', () => {
			notify.ingest({
				id: 'srv-1',
				severity: 'error',
				category: 'api_error',
				message: 'API error: HTTP 500',
			});

			const row = notificationHistory.current.find((n) => n.id === 'srv-1');
			expect(row).toBeTruthy();
			expect(row?.source).toBe('server');
			expect(row?.persist).toBe(true);
			expect(get(unreadCount)).toBe(1);
		});

		it('dedupes by id so a re-ingested event does not double-count', () => {
			notify.ingest({ id: 'srv-dup', severity: 'warning', message: 'Rate limited' });
			notify.ingest({ id: 'srv-dup', severity: 'warning', message: 'Rate limited' });

			expect(notificationHistory.current.filter((n) => n.id === 'srv-dup')).toHaveLength(1);
		});
	});

	describe('unread badge', () => {
		it('counts unread history items and clears on markAllRead', () => {
			notify.error('one');
			notify.error('two');
			expect(get(unreadCount)).toBe(2);

			notify.markAllRead();
			expect(get(unreadCount)).toBe(0);
		});
	});
});

/** Tiny helper mirroring svelte/store `get` for the project's createDerived stores. */
function get<T>(store: { current: T }): T {
	return store.current;
}
