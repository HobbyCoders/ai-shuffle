/**
 * Assistant mode asks for approval in words, not with a dialog.
 *
 * A pending permission request in the assistant's home renders as the
 * assistant's own short question, and the owner answers in the message box
 * ("yes" / "no"). Deciding what the reply means is the backend's job; this only
 * turns the request the view already holds into one plain sentence.
 */
import type { PermissionRequest } from '$lib/api/client';
import { displayToolName } from './tool-icons';

function text(value: unknown): string {
	return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

function clip(value: string, max = 120): string {
	return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function code(value: string): string {
	// A backtick inside would end the inline code early.
	return `\`${clip(value).replace(/`/g, "'")}\``;
}

/** What the request would do, as the object of "May I …". */
function action(request: Pick<PermissionRequest, 'tool_name' | 'tool_input'>): string {
	const name = displayToolName(request.tool_name ?? '');
	const input = request.tool_input ?? {};
	const command = text(input.command);
	const path = text(input.file_path) || text(input.path) || text(input.notebook_path);
	const url = text(input.url);
	switch (name) {
		case 'Bash':
		case 'BashOutput':
		case 'Monitor':
			return command ? `run ${code(command)}` : 'run a command';
		case 'Write':
			return path ? `write ${code(path)}` : 'write a file';
		case 'Edit':
		case 'MultiEdit':
		case 'NotebookEdit':
			return path ? `change ${code(path)}` : 'change a file';
		case 'WebFetch':
			return url ? `open ${url}` : 'open a web page';
		default: {
			const description = text(input.description);
			if (description) return clip(description.charAt(0).toLowerCase() + description.slice(1));
			return name ? `use ${name}` : 'go ahead';
		}
	}
}

/** "May I run `npm test`? Yes or no." — used when the server sent no wording. */
export function assistantConfirmationText(request: Pick<PermissionRequest, 'tool_name' | 'tool_input'>): string {
	return `May I ${action(request)}? Yes or no.`;
}

/** A request the assistant asks in words; it never opens the approval dialog. */
export function isAssistantConfirm(request: Pick<PermissionRequest, 'assistant_confirm'>): boolean {
	return !!request.assistant_confirm && typeof request.assistant_confirm === 'object';
}

/**
 * The one question on screen: the oldest in-conversation request (the server
 * settles them one at a time, oldest first), in the server's own words.
 */
export function currentAssistantQuestion(requests: readonly PermissionRequest[]): { request: PermissionRequest; text: string } | null {
	const request = requests.find(isAssistantConfirm);
	if (!request) return null;
	const question = request.assistant_confirm?.question?.trim();
	return { request, text: question || assistantConfirmationText(request) };
}
