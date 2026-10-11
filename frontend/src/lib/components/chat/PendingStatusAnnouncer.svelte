<script lang="ts">
	/**
	 * PendingStatusAnnouncer — the one screen-reader channel for unsend verdicts
	 * on pending messages (#2275).
	 *
	 * PendingMessages renders inside the transcript's `role="log"`, which is
	 * itself a live region and goes `aria-busy` while a reply streams. A status
	 * region nested in there is suppressed while busy, and a withdrawn message's
	 * region is removed before it can speak. So the chips stay silent and this
	 * persistent, visually hidden region — mounted OUTSIDE the log — announces
	 * each transition instead:
	 *
	 *   no status → cancelling          "Cancelling message…"
	 *   cancelling → delivered          "Message already sent"
	 *   cancelling → gone               "Message withdrawn"
	 *
	 * A cancelling entry that drops back to no status (the store could not
	 * confirm the cancel) is announced by the error path, not here. Switching
	 * sessions resets what was seen so another chat's queue is never announced.
	 */
	import type { QueuedMessage } from '$lib/stores/types';

	interface Props {
		messages: QueuedMessage[];
		/** Session the messages belong to; a change starts a fresh baseline. */
		sessionId?: string | null;
	}

	let { messages, sessionId = null }: Props = $props();

	let announcement = $state('');
	let seen = new Map<string, QueuedMessage['status']>();
	let seenSession: string | null | undefined = undefined;

	function describe(prev: Map<string, QueuedMessage['status']>, next: QueuedMessage[]): string | null {
		const nextIds = new Set<string>();
		let text: string | null = null;
		for (const msg of next) {
			nextIds.add(msg.id);
			const before = prev.get(msg.id);
			if (msg.status === before) continue;
			if (msg.status === 'cancelling') text = 'Cancelling message…';
			else if (msg.status === 'delivered') text = 'Message already sent';
		}
		for (const [id, status] of prev) {
			if (status === 'cancelling' && !nextIds.has(id)) text = 'Message withdrawn';
		}
		return text;
	}

	$effect(() => {
		const next = messages;
		if (sessionId !== seenSession) {
			seenSession = sessionId;
			seen = new Map(next.map((m) => [m.id, m.status]));
			announcement = '';
			return;
		}
		const text = describe(seen, next);
		seen = new Map(next.map((m) => [m.id, m.status]));
		if (text) {
			// The same words twice in a row must still be spoken: a trailing
			// no-break space changes the node's text without changing the speech.
			announcement = announcement === text ? `${text}\u00a0` : text;
		}
	});
</script>

<div class="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
