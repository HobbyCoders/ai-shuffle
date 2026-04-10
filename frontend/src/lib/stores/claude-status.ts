/**
 * Claude service status store.
 *
 * Polls the backend every 3 minutes for Claude's service status.
 * Uses plain .ts (NOT .svelte.ts) to avoid dual module evaluation issues.
 * See KNOWN_ISSUES.md Root Cause 10.
 *
 * Dismiss state is persisted to localStorage using an incident fingerprint.
 * The banner only reappears when new or changed incidents are detected,
 * NOT on every page refresh.
 */

import { getClaudeStatus, type ClaudeStatusResponse } from '$lib/api/claude-status';

const POLL_INTERVAL_MS = 3 * 60 * 1000; // 3 minutes
const STORAGE_KEY = 'claude-status-dismissed-fingerprint';

export interface ClaudeStatusState {
	status: ClaudeStatusResponse | null;
	loading: boolean;
	dismissed: boolean;
}

type Subscriber = (state: ClaudeStatusState) => void;

/**
 * Build a stable fingerprint from current incidents.
 * Uses incident name + started_at to uniquely identify each incident,
 * plus the latest_update to detect meaningful changes to existing incidents.
 */
function _buildFingerprint(status: ClaudeStatusResponse): string {
	if (status.ok || status.incidents.length === 0) return '';
	return status.incidents
		.map((i) => `${i.name}|${i.started_at}|${i.latest_update ?? ''}`)
		.sort()
		.join(';;');
}

function _getSavedFingerprint(): string | null {
	try {
		return localStorage.getItem(STORAGE_KEY);
	} catch {
		return null;
	}
}

function _saveFingerprint(fp: string) {
	try {
		localStorage.setItem(STORAGE_KEY, fp);
	} catch {
		// localStorage unavailable — dismiss won't persist
	}
}

function _clearFingerprint() {
	try {
		localStorage.removeItem(STORAGE_KEY);
	} catch {
		// ignore
	}
}

let _state: ClaudeStatusState = {
	status: null,
	loading: false,
	dismissed: false,
};

const _subscribers = new Set<Subscriber>();
let _pollTimer: ReturnType<typeof setInterval> | null = null;
let _started = false;

function _notify() {
	const snapshot = { ..._state };
	for (const fn of _subscribers) {
		try {
			fn(snapshot);
		} catch {
			// Subscriber threw — ignore
		}
	}
}

function _setState(partial: Partial<ClaudeStatusState>) {
	_state = { ..._state, ...partial };
	_notify();
}

/** Compare two status responses to avoid no-op reactive cascades during polling. */
function _statusUnchanged(a: ClaudeStatusResponse | null, b: ClaudeStatusResponse | null): boolean {
	if (a === b) return true;
	if (!a || !b) return false;
	if (a.ok !== b.ok) return false;
	if (a.incidents.length !== b.incidents.length) return false;
	// Compare fingerprints — captures name + started_at + latest_update
	return _buildFingerprint(a) === _buildFingerprint(b);
}

async function _fetchStatus() {
	try {
		_setState({ loading: true });
		const status = await getClaudeStatus();
		const fingerprint = _buildFingerprint(status);

		// Status is ok — clear any saved dismiss and show as not-dismissed
		if (status.ok) {
			// Skip update if status hasn't changed — prevents idle GC pressure
			if (_statusUnchanged(status, _state.status)) {
				_setState({ loading: false });
				return;
			}
			_clearFingerprint();
			_setState({ status, loading: false, dismissed: false });
			return;
		}

		// Status is NOT ok — check if user already dismissed this exact set of incidents
		const savedFp = _getSavedFingerprint();
		const isDismissed = savedFp !== null && savedFp === fingerprint;

		// Skip update if nothing meaningful changed
		if (_statusUnchanged(status, _state.status) && _state.dismissed === isDismissed) {
			_setState({ loading: false });
			return;
		}

		_setState({ status, loading: false, dismissed: isDismissed });
	} catch {
		_setState({ loading: false });
	}
}

function _handleVisibility() {
	if (document.hidden) {
		_stopPoll();
	} else {
		_fetchStatus();
		_startPoll();
	}
}

function _startPoll() {
	if (_pollTimer) return;
	_pollTimer = setInterval(_fetchStatus, POLL_INTERVAL_MS);
}

function _stopPoll() {
	if (_pollTimer) {
		clearInterval(_pollTimer);
		_pollTimer = null;
	}
}

export const claudeStatus = {
	get current(): ClaudeStatusState {
		return _state;
	},

	subscribe(fn: Subscriber): () => void {
		_subscribers.add(fn);
		fn({ ..._state });
		return () => _subscribers.delete(fn);
	},

	/** Start polling. Call once from the root layout. */
	start() {
		if (_started) return;
		_started = true;
		_fetchStatus();
		_startPoll();
		document.addEventListener('visibilitychange', _handleVisibility);
	},

	/** Stop polling. Call on root layout destroy. */
	stop() {
		_started = false;
		_stopPoll();
		document.removeEventListener('visibilitychange', _handleVisibility);
	},

	/** Dismiss the banner. Persists to localStorage so it survives page refreshes.
	 *  Banner reappears only when incidents change (new incident or updated status). */
	dismiss() {
		if (_state.status) {
			const fp = _buildFingerprint(_state.status);
			if (fp) _saveFingerprint(fp);
		}
		_setState({ dismissed: true });
	},

	/** Force a refresh right now. */
	async refresh() {
		await _fetchStatus();
	},
};
