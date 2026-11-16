/**
 * SvelteKit 3's `pushState` and `replaceState` are asynchronous: a router that
 * is not ready (early boot, tests) rejects the promise instead of throwing.
 * Callers here already treat that as "no history entry", so the rejection is
 * swallowed rather than left unhandled.
 */
export function ignoreRouterNotReady(result: Promise<void> | void): void {
	if (result) result.catch(() => {});
}
