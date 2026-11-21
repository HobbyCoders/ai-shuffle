/**
 * vault — what the Vault for the active chat is showing.
 *
 * VaultPanel sits above the composer and owns the polling, the request cards
 * and the panel that lists active access. The Vault chip in the composer row
 * (VaultChip) and the phone's prompt dock read the state from here and open
 * that panel through `open` / `toggle`. VaultPanel publishes the summary
 * whenever its snapshot changes and clears the entry when it unmounts.
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
	/** Saved sign-ins the chat can offer. */
	saved: number;
	/**
	 * The device a mirrored conversation runs on, where its access and saved
	 * sign-ins live; null when it runs on this worker.
	 */
	owner?: string | null;
	/** The panel has something to show: the chip is worth rendering. */
	manageable?: boolean;
	/** DOM id of the panel, for the chip's `aria-controls`. */
	panelId?: string;
	/** The last refresh failed; the panel shows the error. */
	error: boolean;
	/** Open the Vault panel. No-op when there is nothing to show. */
	open: () => void;
	/** Open the panel, or close it when it is open (the chip). */
	toggle?: () => void;
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
