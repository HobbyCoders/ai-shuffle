import { readable } from 'svelte/store';

/** Compatibility for old metadata consumers. Chat archive no longer exists. */
export const archivedChats = {
    subscribe: readable<ReadonlySet<string>>(new Set()).subscribe,
    mark(_sessionId: string, _archived: boolean) { /* retired */ }
};
