/**
 * Whether "Message all active chats" is open. The sidebar owns the dialog
 * (it knows which chats are active); the command palette opens it from here.
 */
export const broadcastDialog = $state({ open: false, hosts: 0 });

/** The sidebar mounts the dialog; returns its unmount. */
export function hostBroadcastDialog(): () => void {
	broadcastDialog.hosts += 1;
	return () => {
		broadcastDialog.hosts -= 1;
	};
}

/** Open it. `reveal` mounts a sidebar first when none is (a closed phone drawer). */
export function openBroadcastDialog(reveal?: () => void) {
	if (broadcastDialog.hosts === 0) reveal?.();
	broadcastDialog.open = true;
}

export function closeBroadcastDialog() {
	broadcastDialog.open = false;
}
