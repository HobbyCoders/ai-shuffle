/**
 * Store Utilities — Centralized subscriber/notify boilerplate.
 *
 * This is a plain .ts file (NOT .svelte.ts) to avoid dual module
 * evaluation issues with Svelte 5 runes.
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
/** Equality predicate a derived store uses to decide whether to notify. */
export type Equals<D> = (a: D, b: D) => boolean;

// ─── Equality helpers ────────────────────────────────────────────────────────

/**
 * Shallow (own enumerable keys, `Object.is` per value) equality.
 *
 * Pass this to `createDerived` for a getter that BUILDS an object or array —
 * `{ sessionId, title }`, a filtered list — where a fresh reference is produced
 * on every read and the default identity check could never short-circuit.
 */
export function shallowEqual<D>(a: D, b: D): boolean {
	if (Object.is(a, b)) return true;
	if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
	if (Array.isArray(a) !== Array.isArray(b)) return false;
	const ak = Object.keys(a as Record<string, unknown>);
	const bk = Object.keys(b as Record<string, unknown>);
	if (ak.length !== bk.length) return false;
	for (const k of ak) {
		if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
		if (!Object.is((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k])) {
			return false;
		}
	}
	return true;
}

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
	 *
	 * EQUALITY SHORT-CIRCUIT (finding W10): the parent notifies on every write of
	 * its whole state object — for `activeSession` that is once per fold, i.e.
	 * many times a second while streaming. A derived subscriber is re-invoked
	 * only when its OWN value actually changed, so a component subscribed to
	 * `activeSessionStreaming` does no work while message text streams in.
	 *
	 * Defaults to `Object.is`, which is the right check for the common
	 * scalar/reference-stable getter. Pass `shallowEqual` (or a custom
	 * predicate) for a getter that builds a fresh object or array each read.
	 */
	function createDerived<D>(getter: () => D, equals: Equals<D> = Object.is) {
		return {
			get current(): D {
				return getter();
			},
			subscribe(fn: Subscriber<D>): Unsubscribe {
				// Seeded from the value delivered on subscribe, so the first
				// parent notification after an unchanged write is already skipped.
				let last = untrack(() => getter());
				const handler: Subscriber<T> = () => {
					const next = getter();
					if (equals(last, next)) return;
					last = next;
					fn(next);
				};
				subscribers.add(handler);
				untrack(() => fn(last));
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
	createDerived: <D>(getter: (s: T) => D, equals?: Equals<D>) => {
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

	function createDerived<D>(getter: (s: T) => D, equals?: Equals<D>) {
		return createDerivedInternal(() => getter(state), equals);
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
