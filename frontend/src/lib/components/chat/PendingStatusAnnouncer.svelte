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
	 *
	 * Entries are keyed by their stable client identity (`clientMessageId`),
	 * falling back to the row id: an optimistic echo that becomes its server row
	 * changes `id` but is the same message, and must not read as withdrawn.
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
	const keyOf = (msg: QueuedMessage) => msg.clientMessageId ?? msg.id;
	const snapshot = (list: QueuedMessage[]) => new Map(list.map((m) => [keyOf(m), m.status]));
	let seenSession: string | null | undefined = undefined;

	function describe(prev: Map<string, QueuedMessage['status']>, next: QueuedMessage[]): string | null {
		const nextKeys = new Set<string>();
		let text: string | null = null;
		for (const msg of next) {
			const key = keyOf(msg);
			nextKeys.add(key);
			const before = prev.get(key);
			if (msg.status === before) continue;
			if (msg.status === 'cancelling') text = 'Cancelling message…';
			else if (msg.status === 'delivered') text = 'Message already sent';
		}
		for (const [key, status] of prev) {
			if (status === 'cancelling' && !nextKeys.has(key)) text = 'Message withdrawn';
		}
		return text;
	}

	$effect(() => {
		const next = messages;
		if (sessionId !== seenSession) {
			seenSession = sessionId;
			seen = snapshot(next);
			announcement = '';
			return;
		}
		const text = describe(seen, next);
		seen = snapshot(next);
		if (text) {
			// The same words twice in a row must still be spoken: a trailing
			// no-break space changes the node's text without changing the speech.
			announcement = announcement === text ? `${text}\u00a0` : text;
		}
	});
</script>

<div class="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
