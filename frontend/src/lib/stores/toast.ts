/**
 * Lightweight transient toast store — used by the WS message handler to
 * surface `api_error` events (Slice C) and any future fire-and-forget UI
 * notifications. Plain `.ts` (no runes) — see KNOWN_ISSUES.md Root Cause 10.
 *
 * Each toast auto-dismisses after `duration_ms`. Multiple toasts stack in the
 * order they arrive. The `<ToastViewport>` component renders them.
 */

import { createStore } from './store-utils';

export type ToastTone = 'error' | 'warning' | 'info' | 'success';

/** Optional action button rendered inside a toast (e.g. "Fix with AI"). */
export interface ToastAction {
	label: string;
	onClick: () => void;
}

export interface Toast {
	id: number;
	message: string;
	tone: ToastTone;
	duration_ms: number;
	created_at: number;
	action?: ToastAction;
}

export interface ToastState {
	toasts: Toast[];
}

const _store = createStore<ToastState>({ toasts: [] });
let _nextId = 1;
const _timers = new Map<number, ReturnType<typeof setTimeout>>();

function _schedule(id: number, ms: number) {
	const t = setTimeout(() => {
		toast.dismiss(id);
	}, ms);
	_timers.set(id, t);
}

function _clearTimer(id: number) {
	const t = _timers.get(id);
	if (t) {
		clearTimeout(t);
		_timers.delete(id);
	}
}

export const toast = {
	get current(): ToastState {
		return _store.current;
	},

	subscribe: _store.subscribe,

	push(
		message: string,
		opts: { tone?: ToastTone; duration_ms?: number; action?: ToastAction } = {}
	): number {
		const id = _nextId++;
		const next: Toast = {
			id,
			message,
			tone: opts.tone ?? 'info',
			duration_ms: opts.duration_ms ?? 5000,
			created_at: Date.now(),
			action: opts.action,
		};
		_store.update(s => ({ toasts: [...s.toasts, next] }));
		_schedule(id, next.duration_ms);
		return id;
	},

	dismiss(id: number): void {
		_clearTimer(id);
		_store.update(s => ({ toasts: s.toasts.filter(t => t.id !== id) }));
	},

	dismissAll(): void {
		for (const id of _timers.keys()) _clearTimer(id);
		_store.update(() => ({ toasts: [] }));
	},
};
