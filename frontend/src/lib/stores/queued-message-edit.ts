import { writable } from 'svelte/store';

/**
 * Holds the id of the queued message whose content is currently loaded in the
 * composer for editing. Editing a queued message no longer removes it from the
 * queue up front — the queued row is only removed from the server queue when the
 * edited message is actually sent (or re-queued), so a stray tap can never lose
 * a queued turn. `null` means nothing is being edited.
 */
export const queuedMessageEdit = writable<{ id: string } | null>(null);
