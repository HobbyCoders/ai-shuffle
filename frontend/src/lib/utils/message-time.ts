/**
 * Format a message timestamp for display in chat bubble headers.
 *
 * Accepts the ISO-8601 string persisted by the backend (message
 * `metadata.timestamp` → `ChatMessage.timestamp`) or a Date. Falls back to
 * the current time only when no usable timestamp exists — e.g. a live
 * message from the current turn that hasn't been persisted yet.
 */
export function formatMessageTime(timestamp?: Date | string): string {
	let d: Date;
	if (timestamp instanceof Date) {
		d = timestamp;
	} else if (timestamp) {
		d = new Date(timestamp);
	} else {
		d = new Date();
	}
	if (isNaN(d.getTime())) {
		d = new Date();
	}
	return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
}
