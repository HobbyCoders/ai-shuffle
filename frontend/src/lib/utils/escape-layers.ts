/**
 * Escape layering: one Escape closes only the thing on top.
 *
 * The rule has two halves:
 * - Whatever consumes an Escape (a dialog, popover, menu, or an input that
 *   cancels its edit) calls `event.preventDefault()`. `stopPropagation()` is
 *   not enough: it does not stop other listeners on the same target (most of
 *   these listen on `window`).
 * - A background layer that closes on a bare Escape (the session side panel)
 *   uses `whenEscapeUnclaimed`. It waits until the key event has reached every
 *   listener and acts only if none of them claimed it, so it does not matter
 *   whether the popover's listener was attached before or after the panel's.
 */

/**
 * Run `act` after `event` has finished dispatching, unless the event is not an
 * Escape or a listener called `preventDefault()` on it. Returns a function that
 * cancels the pending check (call it when the owner unmounts).
 */
export function whenEscapeUnclaimed(event: KeyboardEvent, act: () => void): () => void {
	if (event.key !== 'Escape' || event.defaultPrevented) return () => {};
	// A task, not a microtask: browsers run microtasks between listeners of a
	// native event, so only a task is sure to run after the last one.
	const timer = setTimeout(() => {
		if (!event.defaultPrevented) act();
	}, 0);
	return () => clearTimeout(timer);
}
