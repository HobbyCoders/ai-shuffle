/**
 * vault — what the Vault panel for the active chat is showing.
 *
 * VaultPanel owns the polling, the request cards and the header panel. The
 * chat's own prompts (VaultPrompt above the composer, the phone's
 * prompt dock) read what is waiting from here and open that panel through
 * `open`. VaultPanel publishes the summary whenever its snapshot changes and
 * clears the entry when it unmounts.
 */
import { writable } from 'svelte/store';

/** One thing in the panel that waits for the owner, as the chat shows it. */
export interface PendingAccessItem {
	id: string;
	/** `machine` / `folder` / `service` for a request; `action` for a one-shot approval. */
	kind: 'machine' | 'folder' | 'service' | 'action';
	/** The agent's stated purpose, or what the one-shot action does. */
	detail: string;
	/** Title of the subagent that asks, when the item is a subagent's shown in its main chat. */
	subagent?: string;
}

export interface VaultSummary {
	sessionId: string;
	/** Requests + one-shot actions waiting for the user. */
	pending: number;
	/** The waiting items themselves, so the chat can show them (#1807). */
	items?: PendingAccessItem[];
	/** Grants currently active in this chat. */
	active: number;
	/** Saved connections listed in the panel. */
	saved: number;
	/** The last refresh failed; the panel shows the error. */
	error: boolean;
	/** Open the Vault panel. No-op when there is nothing to show. */
	open: () => void;
	/**
	 * Answer a waiting one-shot approval (`kind: 'action'`) without opening the
	 * panel. Requests that need private inputs still go through `open`.
	 */
	decide?: (id: string, allow: boolean) => Promise<boolean>;
}

/** Summary for the mounted VaultPanel instance, or null when none is mounted. */
export const vaultSummary = writable<VaultSummary | null>(null);

/**
 * Vault items waiting for the owner, per session (#1807). The open
 * chat's panel writes its own count after every snapshot; the worker's
 * `vault_changed` event carries the count for every other session. The
 * sidebar reads it to put a waiting session under "Needs you". Kept apart from
 * the permission/question attention map, which the server replaces wholesale.
 */
export const vaultPending = writable<Map<string, number>>(new Map());

export function setVaultPending(sessionId: string, count: number): void {
	vaultPending.update((map) => {
		const current = map.get(sessionId) ?? 0;
		const next = Math.max(0, Math.floor(count)) || 0;
		if (current === next) return map;
		const copy = new Map(map);
		if (next > 0) copy.set(sessionId, next);
		else copy.delete(sessionId);
		return copy;
	});
}
