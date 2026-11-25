/**
 * Whether "Message all active chats" is open. The sidebar owns the dialog
 * (it knows which chats are active); the command palette opens it from here.
 * When no sidebar is mounted (a collapsed desktop sidebar, a phone drawer not
 * opened yet), each shell sees `open` with no host and mounts its sidebar.
 */
export const broadcastDialog = $state({ open: false, hosts: 0 });

/** The sidebar mounts the dialog; returns its unmount. */
export function hostBroadcastDialog(): () => void {
	broadcastDialog.hosts += 1;
	return () => {
		broadcastDialog.hosts -= 1;
	};
}

export function openBroadcastDialog() {
	broadcastDialog.open = true;
}

export function closeBroadcastDialog() {
	broadcastDialog.open = false;
}
