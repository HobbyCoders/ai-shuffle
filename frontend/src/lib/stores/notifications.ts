import { createStore } from './store-utils';
import { api } from '$lib/api/client';
import { auth } from './auth.svelte';

export type NotificationSeverity = 'error' | 'warning' | 'info' | 'success';
export type NotificationSurface = 'notification' | 'banner';
type NotificationSource = 'client' | 'server';

/** Optional action button rendered inside a banner / center row. */
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
	/** A second, quieter action next to `action`. */
	secondaryAction?: NotificationAction;
	/** Whether this item is saved remotely; notifications always appear in the bell. */
	persist: boolean;
	read: boolean;
	dismissed: boolean;
	created_at: number;
	/** Banner auto-dismiss in ms. 0 / undefined for banners = sticky. */
	duration_ms?: number;
	session_id?: string;
}

/** A notification row as returned by `GET /api/v1/notifications` (wire shape). */
export interface ServerNotificationRow {
	id: string;
	severity: NotificationSeverity;
	surface?: NotificationSurface | 'toast' | 'inline';
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
	/** Currently rendered, auto-dismissing surfaces (page banners). */
	live: AppNotification[];
	/** Persisted history, newest first — drives the NotificationCenter. */
	history: AppNotification[];
}

/** Options accepted by the public `notify.*` API. */
interface NotifyOptions {
	severity?: NotificationSeverity;
	category?: string;
	title?: string;
	action?: NotificationAction;
	secondaryAction?: NotificationAction;
	/** Override the default remote persistence. */
	persist?: boolean;
	/** Banner auto-dismiss in ms. Pass 0 for a sticky banner. */
	duration_ms?: number;
	session_id?: string;
}

/**
 * Rebuilds the buttons of notifications loaded from history (closures cannot
 * be stored). Registered by the app shell; see `historyNotificationActions`
 * in $lib/services/error-actions. `ready` runs alongside the fetch.
 */
export interface HistoryActions {
	ready?: () => Promise<unknown>;
	build: (row: ServerNotificationRow) => { action?: NotificationAction; secondaryAction?: NotificationAction };
}

const HISTORY_CAP = 200;
let _historyActions: HistoryActions | null = null;

const _store = createStore<NotificationState>({ live: [], history: [] });
const _timers = new Map<string, ReturnType<typeof setTimeout>>();
const _pendingRemote = new Set<Promise<unknown>>();
let _historyRequest = 0;

function _trackRemote(promise: Promise<unknown>): void {
	const settled = promise.catch(() => {}).finally(() => _pendingRemote.delete(settled));
	_pendingRemote.add(settled);
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
		_trackRemote(api
			.post('/notifications', {
				id: n.id,
				severity: n.severity,
				surface: n.surface,
				category: n.category,
				title: n.title,
				message: n.message,
				session_id: n.session_id,
			})
			);
	} catch {
		// Notification persistence must never break the app.
	}
}

/** Remove a live (page banner) notification. Leaves history untouched. */
function dismissLive(id: string): void {
	_clearTimer(id);
	_store.update((s) => ({ ...s, live: s.live.filter((n) => n.id !== id) }));
}

/**
 * Core entry point. Builds a notification, renders it as a banner or bell item and,
 * when persisted, records it in history (+ POSTs client-originated ones).
 */
function _emit(
	opts: NotifyOptions & {
		surface?: NotificationSurface;
		message: string;
		source?: NotificationSource;
		id?: string;
		read?: boolean;
		/** Server-supplied timestamp; falls back to now for client-originated. */
		created_at?: number;
	}
): string {
	const severity = opts.severity ?? 'info';
	const surface = opts.surface === 'banner' ? 'banner' : 'notification';
	const source = opts.source ?? 'client';
	const persist = opts.persist ?? (surface === 'notification');
	const id = opts.id ?? `c-${Array.from(globalThis.crypto.getRandomValues(new Uint32Array(4)), (part) => part.toString(16).padStart(8, '0')).join('')}`;
	if (opts.source === 'server' && [..._store.current.history, ..._store.current.live].some((n) => n.id === id)) return id;
	const duration_ms = opts.duration_ms ?? 0;

	const n: AppNotification = {
		id,
		severity,
		surface,
		category: opts.category,
		source,
		title: opts.title,
		message: opts.message ?? '',
		action: opts.action,
		secondaryAction: opts.secondaryAction,
		persist,
		read: opts.read ?? false,
		dismissed: false,
		created_at: opts.created_at ?? Date.now(),
		duration_ms,
		session_id: opts.session_id,
	};

	_store.update((s) => {
		const live = surface === 'banner' ? [...s.live.filter((b) => b.id !== id), n] : s.live;
		let history = s.history;
		if (surface === 'notification' || persist) {
			// Dedupe by id (e.g. an ingested server event we already hold).
			history = [n, ...history.filter((h) => h.id !== id)].slice(0, HISTORY_CAP);
		}
		return { live, history };
	});

	if (surface === 'banner' && duration_ms > 0) {
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
 * notify.success('Saved');
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
				surface?: NotificationSurface | 'toast' | 'inline';
				category?: string;
				title?: string;
				message: string;
				duration_ms?: number;
				session_id?: string;
				read?: boolean;
				persist?: boolean;
				created_at?: number;
			},
			action?: NotificationAction,
			secondaryAction?: NotificationAction
		): string =>
			_emit({
				...payload,
				surface: payload.surface === 'banner' ? 'banner' : 'notification',
				source: 'server',
				persist: payload.persist ?? payload.surface !== 'banner',
				action,
				secondaryAction,
			}),

		/** Reconcile the server snapshot while retaining changes made during its fetch. */
		hydrate: (items: AppNotification[], baseline = _store.current.history) => {
			_store.update((s) => {
				const before = new Map(baseline.map(n => [n.id, n]));
				const current = new Map(s.history.map(n => [n.id, n]));
				const merged = new Map<string, AppNotification>();
				for (const item of items) {
					const local = current.get(item.id);
					if (before.has(item.id) && !local) continue; // dismissed during fetch
					if (local && before.get(item.id) !== local) { merged.set(item.id, local); continue; }
					if (!item.dismissed) merged.set(item.id, { ...item, surface: 'notification',
						action: local?.action ?? item.action, secondaryAction: local?.secondaryAction ?? item.secondaryAction,
						session_id: item.session_id ?? local?.session_id });
				}
				for (const local of s.history) {
					if (!local.persist || before.get(local.id) !== local) merged.set(local.id, local);
				}
				return { ...s, history: [...merged.values()].sort((a, b) => b.created_at - a.created_at).slice(0, HISTORY_CAP) };
			});
		},

		/**
		 * Fetch persisted history from the backend and seed the bell. Called on
		 * app load (see +layout.svelte). Best-effort: guards SSR + auth and never
		 * throws so it can't break startup. Server rows carry their own
		 * `{kind, payload}` action descriptor which can't be rendered as a live
		 * button; the registered `HistoryActions` rebuild the buttons the live
		 * event would have had (e.g. "Send report to support" on an error).
		 */
		loadHistory: async (): Promise<void> => {
			try {
				if (typeof window === 'undefined') return;
				if (!auth.current.status?.authenticated) return;
				const request = ++_historyRequest;
				const owner = auth.current.status.preference_identity;
				await Promise.allSettled([..._pendingRemote]);
				if (request !== _historyRequest) return;
				const baseline = _store.current.history;
				const actions = _historyActions;
				const [res] = await Promise.all([
					api.get<{ notifications?: ServerNotificationRow[] }>('/notifications'),
					actions?.ready?.().catch(() => {})
				]);
				if (request !== _historyRequest || !auth.current.status?.authenticated || owner !== auth.current.status.preference_identity) return;
				const rows = res?.notifications ?? [];
				const items: AppNotification[] = rows.map((r) => ({
					...actions?.build(r),
					id: r.id,
					severity: r.severity,
					surface: r.surface === 'banner' ? 'banner' : 'notification',
					category: r.category ?? undefined,
					source: 'server',
					title: r.title ?? undefined,
					message: r.message ?? '',
					persist: true,
					read: !!r.read,
					dismissed: !!r.dismissed,
					created_at: typeof r.created_at === 'number' ? r.created_at : 0,
					session_id: r.session_id ?? undefined,
				}));
				notify.hydrate(items, baseline);
			} catch {
				// Never break startup on a notifications fetch failure.
			}
		},

		/** Register how history items get their buttons back (null to stop). */
		setHistoryActions: (actions: HistoryActions | null) => {
			_historyActions = actions;
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

		/** Dismiss a live banner without touching history. */
		dismissLive,
		/** Account changes clear only local presentation, never the old account's history. */
		resetLocal: () => {
			_historyRequest++;
			for (const id of _timers.keys()) _clearTimer(id);
			_pendingRemote.clear();
			_store.update(() => ({live:[], history:[]}));
		},
	}
);

/** Run a remote mutation best-effort: guard SSR + auth, swallow failures. */
function _remote(fn: () => Promise<unknown>): void {
	try {
		if (typeof window === 'undefined') return;
		if (!auth.current.status?.authenticated) return;
		_trackRemote(fn());
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


/** Live page banners only — consumed by the global banner host. */
export const liveBanners = _store.createDerived((s) => s.live.filter((n) => n.surface === 'banner'));

/** Persisted history (newest first) — consumed by NotificationCenter. */
export const notificationHistory = _store.createDerived((s) => s.history);

/** Unread count for the bell badge. */
export const unreadCount = _store.createDerived((s) => s.history.filter((n) => !n.read).length);
