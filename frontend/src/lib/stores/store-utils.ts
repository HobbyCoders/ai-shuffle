/**
 * Store Utilities — Centralized subscriber/notify boilerplate.
 *
 * This is a plain .ts file (NOT .svelte.ts) to avoid dual module
 * evaluation issues with Svelte 5 runes. See KNOWN_ISSUES.md Root Cause 10.
 *
 * Does NOT use $state, $derived, or $effect runes.
 *
 * Provides:
 * - createStore<T>(): Full store with subscribe/set/update/createDerived
 * - createSubscribers<T>(): Lightweight subscriber machinery only, for stores
 *   that manage their own state (e.g. $state in .svelte.ts files)
 * - withLoading<S, T>(): Wraps an async operation with loading/error flag management
 */

import { untrack } from 'svelte';

// ─── Types ──────────────────────────────────────────────────────────────────

type Subscriber<T> = (value: T) => void;
type Unsubscribe = () => void;

// ─── withLoading ─────────────────────────────────────────────────────────────

/**
 * Constraint for states that withLoading can manage.
 * The state must have `loading: boolean` and `error: string | null`.
 */
export type LoadingState = { loading: boolean; error: string | null };

/**
 * Wraps an async operation with loading/error flag management.
 *
 * Sets `loading = true, error = null` before the operation, clears
 * `loading = false` on success, and sets `loading = false, error = <message>`
 * on failure — then re-throws so the caller can handle it.
 *
 * Usage in a .svelte.ts store:
 * ```ts
 * async loadItems(): Promise<Item[]> {
 *     return withLoading(_update, () => api.getItems(), 'Failed to load items');
 * }
 * ```
 *
 * @param update  - The store's `_update` function: `(fn: (s: S) => S) => void`
 * @param operation - The async operation to perform
 * @param errorMessage - Fallback message string, or a function `(e: unknown) => string`
 *                       for stores that do custom error extraction
 */
export async function withLoading<S extends LoadingState, T>(
	update: (fn: (s: S) => S) => void,
	operation: () => Promise<T>,
	errorMessage: string | ((e: unknown) => string)
): Promise<T> {
	update((s) => ({ ...s, loading: true, error: null }));
	try {
		const result = await operation();
		update((s) => ({ ...s, loading: false }));
		return result;
	} catch (e) {
		const error = typeof errorMessage === 'function'
			? errorMessage(e)
			: (e instanceof Error ? e.message : errorMessage);
		update((s) => ({ ...s, error, loading: false }));
		throw e;
	}
}

// ─── createSubscribers ──────────────────────────────────────────────────────

/**
 * Lightweight subscriber machinery for stores that manage their own state
 * (e.g. stores in .svelte.ts files that use $state for the actual value).
 *
 * Returns helpers to add/remove/notify subscribers and create derived stores,
 * but does NOT own the state itself — the caller provides the current value
 * via the `getCurrentValue` getter.
 *
 * Usage in a .svelte.ts file:
 * ```ts
 * let _state = $state<MyState>(initial);
 * const { subscribe, notifySubscribers, update, createDerived } =
 *     createSubscribers(() => _state, (v) => { _state = v; });
 * ```
 */
export function createSubscribers<T>(
	getCurrentValue: () => T,
	setValue: (v: T) => void
) {
	const subscribers = new Set<Subscriber<T>>();

	function notifySubscribers(): void {
		untrack(() => {
			const value = getCurrentValue();
			for (const fn of subscribers) fn(value);
		});
	}

	function subscribe(fn: Subscriber<T>): Unsubscribe {
		subscribers.add(fn);
		untrack(() => fn(getCurrentValue()));
		return () => {
			subscribers.delete(fn);
		};
	}

	function update(fn: (s: T) => T): void {
		setValue(fn(untrack(() => getCurrentValue())));
		notifySubscribers();
	}

	function set(value: T): void {
		setValue(value);
		notifySubscribers();
	}

	/**
	 * Create a derived store that piggybacks on this store's subscribers.
	 * The getter receives no arguments — it should close over the parent state.
	 */
	function createDerived<D>(getter: () => D) {
		return {
			get current(): D {
				return getter();
			},
			subscribe(fn: Subscriber<D>): Unsubscribe {
				const handler: Subscriber<T> = () => fn(getter());
				subscribers.add(handler);
				untrack(() => fn(getter()));
				return () => {
					subscribers.delete(handler);
				};
			}
		};
	}

	return {
		subscribe,
		notifySubscribers,
		update,
		set,
		createDerived,
		/** Expose the raw subscriber set for edge cases (e.g. notify without untrack) */
		_subscribers: subscribers,
	};
}

// ─── createStore ────────────────────────────────────────────────────────────

interface Store<T> {
	readonly current: T;
	subscribe: (fn: Subscriber<T>) => Unsubscribe;
	update: (fn: (s: T) => T) => void;
	set: (value: T) => void;
	createDerived: <D>(getter: (s: T) => D) => {
		readonly current: D;
		subscribe: (fn: Subscriber<D>) => Unsubscribe;
	};
	notifySubscribers: () => void;
	/** Direct state access for stores that need mutation outside update() */
	_getState: () => T;
	_setState: (s: T) => void;
	_subscribers: Set<Subscriber<T>>;
}

/**
 * Full store factory for plain .ts files that don't use $state.
 *
 * Owns the state value internally and provides get/set/update/subscribe.
 *
 * Usage:
 * ```ts
 * const store = createStore<MyState>(initialState);
 * export const myStore = {
 *     get current() { return store.current; },
 *     subscribe: store.subscribe,
 *     doSomething() {
 *         store.update(s => ({ ...s, loading: true }));
 *     }
 * };
 * ```
 */
export function createStore<T>(initial: T): Store<T> {
	let state = initial;

	const {
		subscribe,
		notifySubscribers,
		update,
		set,
		createDerived: createDerivedInternal,
		_subscribers,
	} = createSubscribers<T>(
		() => state,
		(v) => { state = v; }
	);

	function createDerived<D>(getter: (s: T) => D) {
		return createDerivedInternal(() => getter(state));
	}

	return {
		get current() { return state; },
		subscribe,
		update,
		set,
		createDerived,
		notifySubscribers,
		_getState: () => state,
		_setState: (s: T) => { state = s; },
		_subscribers,
	};
}
