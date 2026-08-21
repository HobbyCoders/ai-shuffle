export const MAX_COLLAPSED_USER_MESSAGE_LINES = 8;
export const MAX_COLLAPSED_USER_MESSAGE_LENGTH = 600;

/** T3-style guard: only genuinely long prompts get disclosure chrome. */
export function shouldCollapseUserMessage(content: string): boolean {
	if (!content.trim()) return false;
	return (
		content.length > MAX_COLLAPSED_USER_MESSAGE_LENGTH ||
		content.split('\n').length > MAX_COLLAPSED_USER_MESSAGE_LINES
	);
}
