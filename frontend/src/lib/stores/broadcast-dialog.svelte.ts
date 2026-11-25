import type { BroadcastResult } from '$lib/api/broadcast';
import type { BroadcastCandidate } from '$lib/components/layout/sidebar/BroadcastDialog.svelte';

/**
 * Whether "Message all active chats" is open, and what is in it. The sidebar
 * owns the dialog (it knows which chats are active); the command palette opens
 * it from here. When no sidebar is mounted (a collapsed desktop sidebar, a
 * phone drawer not opened yet), each shell sees `open` with no host and mounts
 * its sidebar.
 *
 * The draft lives here, not in the dialog: a resize across the phone
 * breakpoint can unmount one sidebar and mount another while the dialog is
 * open, and the message, the chips and a send already on its way must carry
 * over to the new one.
 */
export interface BroadcastDraft {
	/** Only the chips the user changed; the rest follow their default. */
	picked: Record<string, boolean>;
	message: string;
	sending: boolean;
	error: string | null;
	receipts: { chat: BroadcastCandidate; result: BroadcastResult }[] | null;
}

const freshDraft = (): BroadcastDraft => ({ picked: {}, message: '', sending: false, error: null, receipts: null });

export const broadcastDialog = $state({ open: false, hosts: 0, draft: freshDraft() });

/** The sidebar mounts the dialog; returns its unmount. */
export function hostBroadcastDialog(): () => void {
	broadcastDialog.hosts += 1;
	return () => {
		broadcastDialog.hosts -= 1;
	};
}

/** A fresh dialog every time it opens. */
export function openBroadcastDialog() {
	if (!broadcastDialog.open) resetBroadcastDraft();
	broadcastDialog.open = true;
}

export function closeBroadcastDialog() {
	broadcastDialog.open = false;
}

export function resetBroadcastDraft() {
	Object.assign(broadcastDialog.draft, freshDraft());
}
