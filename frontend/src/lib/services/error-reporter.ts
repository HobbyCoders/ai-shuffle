/**
 * Client-side error reporter.
 *
 * Fire-and-forget POSTs frontend / WS / API errors to
 * ``/api/v1/logs/client-error`` so they land in the same durable ``error_log``
 * store the backend writes to — the store an agent reads via the
 * ``GetAppErrors`` MCP tool. See ``app/api/logs.py`` + ``app/db/error_logs.py``.
 *
 * Guards:
 * - **Rate-limit / dedupe**: a repeat of the same (source, message, status)
 *   within ``_DEDUPE_WINDOW_MS`` is dropped, so a render loop can't spam the
 *   backend. The dedupe map is bounded so it can't grow without limit.
 * - **Auth gate**: nothing is POSTed before login (no cookie → 401 noise).
 *   Pre-login JS errors are intentionally not captured (see plan).
 * - **Never throws**: error reporting must not itself break the app, so the
 *   whole body is wrapped and the POST failure is swallowed.
 */

import { api } from '$lib/api/client';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth';

export type ClientErrorSource = 'frontend' | 'ws' | 'api';

export interface ClientErrorInput {
	message: string;
	source?: ClientErrorSource;
	level?: string;
	detail?: string;
	url?: string;
	status_code?: number;
	/** Defaults to the active chat session when omitted. */
	session_id?: string;
	context?: Record<string, unknown>;
}

// Repeats of the same key within this window are dropped.
const _DEDUPE_WINDOW_MS = 10_000;
// Cap the dedupe map so a stream of unique errors can't grow it unbounded.
const _MAX_TRACKED = 200;
// Match the backend column bounds so we don't ship bytes that get truncated.
const _MAX_MESSAGE = 2000;
const _MAX_DETAIL = 20000;

const _recent = new Map<string, number>();

function _dedupeKey(source: string, message: string, status?: number): string {
	return `${source}${message}${status ?? ''}`;
}

function _shouldSend(key: string, now: number): boolean {
	const last = _recent.get(key);
	if (last !== undefined && now - last < _DEDUPE_WINDOW_MS) return false;
	_recent.set(key, now);
	// Bounded cleanup: when the map gets large, drop entries past the window.
	if (_recent.size > _MAX_TRACKED) {
		for (const [k, ts] of _recent) {
			if (now - ts >= _DEDUPE_WINDOW_MS) _recent.delete(k);
		}
	}
	return true;
}

/** Best-effort extraction of a stack trace / detail string from any thrown value. */
export function errorDetail(err: unknown): string | undefined {
	if (err instanceof Error) return err.stack || `${err.name}: ${err.message}`;
	if (typeof err === 'string') return err;
	if (err && typeof err === 'object') {
		try {
			return JSON.stringify(err);
		} catch {
			return undefined;
		}
	}
	return undefined;
}

/**
 * Record a client-side error. Fire-and-forget — returns immediately and never
 * throws. Safe to call from any error path.
 */
export function reportClientError(input: ClientErrorInput): void {
	try {
		if (typeof window === 'undefined') return;
		// Only authenticated sessions can POST; pre-login errors are dropped.
		if (!auth.current.status?.authenticated) return;

		const message = (input.message || '').trim().slice(0, _MAX_MESSAGE);
		if (!message) return;

		const source = input.source ?? 'frontend';
		const now = Date.now();
		if (!_shouldSend(_dedupeKey(source, message, input.status_code), now)) return;

		const sessionId = input.session_id ?? activeSession.current.sessionId ?? undefined;
		const url = input.url ?? (typeof location !== 'undefined' ? location.href : undefined);
		const detail = input.detail ? input.detail.slice(0, _MAX_DETAIL) : undefined;

		const payload = {
			source,
			message,
			level: (input.level ?? 'ERROR').toUpperCase(),
			detail,
			url,
			status_code: input.status_code,
			session_id: sessionId,
			context: input.context,
		};

		// keepalive lets the POST survive a navigation / tab unload triggered by
		// the same error. Failures are swallowed — never feed back into onerror.
		void api.post('/logs/client-error', payload, { keepalive: true }).catch(() => {});
	} catch {
		// Error reporting must never break the app.
	}
}

/**
 * Install ``window.onerror`` + ``unhandledrejection`` listeners that funnel
 * uncaught frontend errors into {@link reportClientError}. Chains any existing
 * ``window.onerror``. Returns a teardown function that restores prior state.
 */
export function installGlobalErrorHandlers(): () => void {
	if (typeof window === 'undefined') return () => {};

	const prevOnError = window.onerror;

	const onError: OnErrorEventHandler = (event, source, lineno, colno, error) => {
		try {
			const message =
				typeof event === 'string' ? event : (error?.message ?? 'Unknown error');
			reportClientError({
				source: 'frontend',
				message,
				detail: errorDetail(error),
				context: {
					script: source,
					line: lineno,
					column: colno,
				},
			});
		} catch {
			// ignore
		}
		// Preserve any handler that was registered before us.
		if (typeof prevOnError === 'function') {
			return prevOnError.call(window, event, source, lineno, colno, error);
		}
		return false;
	};

	const onRejection = (event: PromiseRejectionEvent) => {
		try {
			const reason = event.reason;
			const message =
				reason instanceof Error
					? reason.message
					: typeof reason === 'string'
						? reason
						: 'Unhandled promise rejection';
			reportClientError({
				source: 'frontend',
				message,
				detail: errorDetail(reason),
				context: { kind: 'unhandledrejection' },
			});
		} catch {
			// ignore
		}
	};

	window.onerror = onError;
	window.addEventListener('unhandledrejection', onRejection);

	return () => {
		window.removeEventListener('unhandledrejection', onRejection);
		if (window.onerror === onError) window.onerror = prevOnError;
	};
}
