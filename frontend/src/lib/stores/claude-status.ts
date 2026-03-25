/**
 * Claude service status store.
 *
 * Polls the backend every 3 minutes for Claude's service status.
 * Uses plain .ts (NOT .svelte.ts) to avoid dual module evaluation issues.
 * See KNOWN_ISSUES.md Root Cause 10.
 */

import { getClaudeStatus, type ClaudeStatusResponse } from '$lib/api/claude-status';

const POLL_INTERVAL_MS = 3 * 60 * 1000; // 3 minutes
const DISMISS_COOLDOWN_MS = 30 * 60 * 1000; // 30 minutes before showing again after dismiss

export interface ClaudeStatusState {
	status: ClaudeStatusResponse | null;
	loading: boolean;
	dismissed: boolean;
	dismissedAt: number | null;
}

type Subscriber = (state: ClaudeStatusState) => void;

let _state: ClaudeStatusState = {
	status: null,
	loading: false,
	dismissed: false,
	dismissedAt: null,
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

async function _fetchStatus() {
	try {
		_setState({ loading: true });
		const status = await getClaudeStatus();

		// If status changed from not-ok to ok, auto-clear dismiss
		if (status.ok && _state.dismissed) {
			_setState({ status, loading: false, dismissed: false, dismissedAt: null });
			return;
		}

		// If dismissed but cooldown expired, un-dismiss so the user sees new updates
		if (_state.dismissed && _state.dismissedAt) {
			const elapsed = Date.now() - _state.dismissedAt;
			if (elapsed >= DISMISS_COOLDOWN_MS) {
				_setState({ status, loading: false, dismissed: false, dismissedAt: null });
				return;
			}
		}

		_setState({ status, loading: false });
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

	/** Dismiss the banner. It will reappear after 30 minutes or when status changes. */
	dismiss() {
		_setState({ dismissed: true, dismissedAt: Date.now() });
	},

	/** Force a refresh right now. */
	async refresh() {
		await _fetchStatus();
	},
};
