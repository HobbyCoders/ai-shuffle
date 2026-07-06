/**
 * Unified notification store — the single source of truth for every user-facing
 * banner / toast / notification in the app.
 *
 * One store, one call API (`notify.*`), three live surfaces (toast | banner |
 * inline) and one persisted history (the bell / NotificationCenter).
 *
 * - Client code calls `notify.error/warning/info/success(...)` or `notify.banner(...)`.
 * - Server-originated notifications arrive over the `notification` WS event and
 *   are fed in via `notify.ingest(...)` (already persisted server-side).
 * - Notifications with `persist: true` go to history (the bell) and, when
 *   client-originated, are POSTed to `/api/v1/notifications` so they survive
 *   reloads and sync across devices.
 *
 * Plain `.ts` (no runes) — same constraint as the legacy `toast.ts` it replaces.
 * See KNOWN_ISSUES.md Root Cause 10.
 */

import { createStore } from './store-utils';
import { api } from '$lib/api/client';
import { auth } from './auth.svelte';

export type NotificationSeverity = 'error' | 'warning' | 'info' | 'success';
export type NotificationSurface = 'toast' | 'banner' | 'inline';
type NotificationSource = 'client' | 'server';

/** Optional action button rendered inside a toast / banner / center row. */
export interface NotificationAction {
	label: string;
	onClick: () => void;
}

export interface AppNotification {
	id: string;
	severity: NotificationSeverity;
	surface: NotificationSurface;
	category?: string;
	source: NotificationSource;
	title?: string;
	message: string;
	action?: NotificationAction;
	/** Whether this notification belongs in the persisted history / bell. */
	persist: boolean;
	read: boolean;
	dismissed: boolean;
	created_at: number;
	/** Toast auto-dismiss in ms. 0 / undefined for surface !== 'toast' = sticky. */
	duration_ms?: number;
	session_id?: string;
}

/** A notification row as returned by `GET /api/v1/notifications` (wire shape). */
interface ServerNotificationRow {
	id: string;
	severity: NotificationSeverity;
	surface?: NotificationSurface;
	category?: string | null;
	source?: NotificationSource;
	title?: string | null;
	message: string;
	session_id?: string | null;
	read?: boolean;
	dismissed?: boolean;
	created_at?: number;
	action?: { kind?: string; payload?: Record<string, unknown> } | null;
}

interface NotificationState {
	/** Currently rendered, auto-dismissing surfaces (toasts + page banners). */
	live: AppNotification[];
	/** Persisted history, newest first — drives the NotificationCenter. */
	history: AppNotification[];
}

/** Options accepted by the public `notify.*` API. */
interface NotifyOptions {
	severity?: NotificationSeverity;
	surface?: NotificationSurface;
	category?: string;
	title?: string;
	action?: NotificationAction;
	/** Override the severity-based persist default. */
	persist?: boolean;
	/** Toast auto-dismiss in ms. Pass 0 for a sticky toast. */
	duration_ms?: number;
	session_id?: string;
}

const HISTORY_CAP = 200;
const DEFAULT_TOAST_MS = 5000;

const _store = createStore<NotificationState>({ live: [], history: [] });
const _timers = new Map<string, ReturnType<typeof setTimeout>>();
let _seq = 0;

/** Errors / warnings persist to the bell by default; success / info are live-only. */
function _defaultPersist(severity: NotificationSeverity): boolean {
	return severity === 'error' || severity === 'warning';
}

function _scheduleAutoDismiss(id: string, ms: number) {
	const t = setTimeout(() => dismissLive(id), ms);
	_timers.set(id, t);
}

function _clearTimer(id: string) {
	const t = _timers.get(id);
	if (t) {
		clearTimeout(t);
		_timers.delete(id);
	}
}

/** POST a client-originated, persisted notification so it survives reload / syncs. */
function _persistRemote(n: AppNotification): void {
	try {
		if (typeof window === 'undefined') return;
		if (!auth.current.status?.authenticated) return;
		void api
			.post('/notifications', {
				id: n.id,
				severity: n.severity,
				surface: n.surface,
				category: n.category,
				title: n.title,
				message: n.message,
				session_id: n.session_id,
			})
			.catch(() => {});
	} catch {
		// Notification persistence must never break the app.
	}
}

/** Remove a live (toast / page banner) notification. Leaves history untouched. */
function dismissLive(id: string): void {
	_clearTimer(id);
	_store.update((s) => ({ ...s, live: s.live.filter((n) => n.id !== id) }));
}

/**
 * Core entry point. Builds a notification, renders it live (toast/banner) and,
 * when persisted, records it in history (+ POSTs client-originated ones).
 */
function _emit(
	opts: NotifyOptions & {
		message: string;
		source?: NotificationSource;
		id?: string;
		read?: boolean;
		/** Server-supplied timestamp; falls back to now for client-originated. */
		created_at?: number;
	}
): string {
	const severity = opts.severity ?? 'info';
	const surface = opts.surface ?? 'toast';
	const source = opts.source ?? 'client';
	const persist = opts.persist ?? _defaultPersist(severity);
	const id = opts.id ?? `c${++_seq}`;
	const duration_ms =
		surface === 'toast' ? (opts.duration_ms ?? DEFAULT_TOAST_MS) : (opts.duration_ms ?? 0);

	const n: AppNotification = {
		id,
		severity,
		surface,
		category: opts.category,
		source,
		title: opts.title,
		message: opts.message ?? '',
		action: opts.action,
		persist,
		read: opts.read ?? false,
		dismissed: false,
		created_at: opts.created_at ?? Date.now(),
		duration_ms,
		session_id: opts.session_id,
	};

	_store.update((s) => {
		const live = surface === 'inline' ? s.live : [...s.live, n];
		let history = s.history;
		if (persist) {
			// Dedupe by id (e.g. an ingested server event we already hold).
			history = [n, ...history.filter((h) => h.id !== id)].slice(0, HISTORY_CAP);
		}
		return { live, history };
	});

	if (surface === 'toast' && duration_ms > 0) {
		_scheduleAutoDismiss(id, duration_ms);
	}
	if (persist && source === 'client') {
		_persistRemote(n);
	}
	return id;
}

/**
 * Public notification API. The single call point for the whole app.
 *
 * ```ts
 * notify.error('Failed to save profile');
 * notify.success('Saved', { duration_ms: 3000 });
 * notify.banner('Connection lost — reconnecting…', { severity: 'warning' });
 * ```
 */
export const notify = Object.assign(
	(message: string, opts: NotifyOptions = {}): string => _emit({ ...opts, message }),
	{
		error: (message: string, opts: Omit<NotifyOptions, 'severity'> = {}) =>
			_emit({ ...opts, message, severity: 'error' }),
		warning: (message: string, opts: Omit<NotifyOptions, 'severity'> = {}) =>
			_emit({ ...opts, message, severity: 'warning' }),
		info: (message: string, opts: Omit<NotifyOptions, 'severity'> = {}) =>
			_emit({ ...opts, message, severity: 'info' }),
		success: (message: string, opts: Omit<NotifyOptions, 'severity'> = {}) =>
			_emit({ ...opts, message, severity: 'success' }),
		/** Convenience for a page-level (sticky) banner. */
		banner: (message: string, opts: Omit<NotifyOptions, 'surface'> = {}) =>
			_emit({ ...opts, message, surface: 'banner' }),

		/**
		 * Feed in a server-originated notification (from the `notification` WS
		 * event). It is already persisted server-side, so this never re-POSTs.
		 * Pass `action` to attach a client-side handler keyed on `category`.
		 */
		ingest: (
			payload: {
				id: string;
				severity: NotificationSeverity;
				surface?: NotificationSurface;
				category?: string;
				title?: string;
				message: string;
				duration_ms?: number;
				session_id?: string;
				read?: boolean;
				created_at?: number;
			},
			action?: NotificationAction
		): string =>
			_emit({
				...payload,
				source: 'server',
				persist: true,
				action,
			}),

		/** Seed history from the backend on load. Replaces the history slice. */
		hydrate: (items: AppNotification[]) => {
			_store.update((s) => ({
				...s,
				history: [...items]
					.sort((a, b) => b.created_at - a.created_at)
					.slice(0, HISTORY_CAP),
			}));
		},

		/**
		 * Fetch persisted history from the backend and seed the bell. Called on
		 * app load (see +layout.svelte). Best-effort: guards SSR + auth and never
		 * throws so it can't break startup. Server rows carry their own
		 * `{kind, payload}` action descriptor which can't be rendered as a live
		 * button, so hydrated items have no `action` (they're past events).
		 */
		loadHistory: async (): Promise<void> => {
			try {
				if (typeof window === 'undefined') return;
				if (!auth.current.status?.authenticated) return;
				const res = await api.get<{ notifications?: ServerNotificationRow[] }>('/notifications');
				const rows = res?.notifications ?? [];
				const items: AppNotification[] = rows.map((r) => ({
					id: r.id,
					severity: r.severity,
					surface: r.surface ?? 'toast',
					category: r.category ?? undefined,
					source: 'server',
					title: r.title ?? undefined,
					message: r.message ?? '',
					action: undefined,
					persist: true,
					read: !!r.read,
					dismissed: !!r.dismissed,
					created_at: typeof r.created_at === 'number' ? r.created_at : 0,
					session_id: r.session_id ?? undefined,
				}));
				notify.hydrate(items);
			} catch {
				// Never break startup on a notifications fetch failure.
			}
		},

		/** Mark a single history item read (locally + remote best-effort). */
		markRead: (id: string) => {
			_store.update((s) => ({
				...s,
				history: s.history.map((n) => (n.id === id ? { ...n, read: true } : n)),
			}));
			_remote(() => api.post(`/notifications/${id}/read`));
		},

		/** Mark every history item read. */
		markAllRead: () => {
			_store.update((s) => ({
				...s,
				history: s.history.map((n) => ({ ...n, read: true })),
			}));
			_remote(() => api.post('/notifications/read-all'));
		},

		/** Remove a single notification from history (and from the live stack). */
		dismiss: (id: string) => {
			_clearTimer(id);
			_store.update((s) => ({
				live: s.live.filter((n) => n.id !== id),
				history: s.history.filter((n) => n.id !== id),
			}));
			_remote(() => api.post(`/notifications/${id}/dismiss`));
		},

		/** Clear the entire history. */
		clearAll: () => {
			_store.update((s) => ({ ...s, history: [] }));
			_remote(() => api.delete('/notifications'));
		},

		/** Dismiss a live toast / banner without touching history. */
		dismissLive,
	}
);

/** Run a remote mutation best-effort: guard SSR + auth, swallow failures. */
function _remote(fn: () => Promise<unknown>): void {
	try {
		if (typeof window === 'undefined') return;
		if (!auth.current.status?.authenticated) return;
		void fn().catch(() => {});
	} catch {
		// never break the UI
	}
}

// ─── Store views ──────────────────────────────────────────────────────────────

/** Full notification state (live + history). */
export const notifications = {
	get current(): NotificationState {
		return _store.current;
	},
	subscribe: _store.subscribe,
};

/** Live toasts only — consumed by ToastViewport. */
export const liveToasts = _store.createDerived((s) => s.live.filter((n) => n.surface === 'toast'));

/** Live page banners only — consumed by the global banner host. */
export const liveBanners = _store.createDerived((s) => s.live.filter((n) => n.surface === 'banner'));

/** Persisted history (newest first) — consumed by NotificationCenter. */
export const notificationHistory = _store.createDerived((s) => s.history);

/** Unread count for the bell badge. */
export const unreadCount = _store.createDerived((s) => s.history.filter((n) => !n.read).length);

// ─── Backward-compatible `toast` alias ─────────────────────────────────────────

type ToastTone = NotificationSeverity;

interface ToastAction {
	label: string;
	onClick: () => void;
}

/**
 * Legacy toast facade kept so the existing `toast.push(...)` call sites keep
 * working unchanged. Delegates into the unified store. `push` returns a numeric
 * id (legacy contract) and `dismiss` accepts it.
 */
export const toast = {
	push(
		message: string,
		opts: { tone?: ToastTone; duration_ms?: number; action?: ToastAction } = {}
	): number {
		const n = ++_seq;
		_emit({
			id: `c${n}`,
			message,
			severity: opts.tone ?? 'info',
			surface: 'toast',
			duration_ms: opts.duration_ms,
			action: opts.action,
			// Legacy toasts were ephemeral; preserve that (no bell history).
			persist: false,
		});
		return n;
	},
	dismiss(id: number): void {
		dismissLive(`c${id}`);
	},
	dismissAll(): void {
		_store.update((s) => {
			for (const n of s.live) {
				if (n.surface === 'toast') _clearTimer(n.id);
			}
			return { ...s, live: s.live.filter((n) => n.surface !== 'toast') };
		});
	},
};
