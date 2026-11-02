/**
 * How long an answer to a permission prompt lasts, shared by the desktop
 * prompt (PermissionRequest.svelte) and the phone dock (MobilePromptDock).
 *
 * The server says which lifetimes it can honour for each request in
 * `remember_scopes`:
 * - `none`: this request only.
 * - `session`: every later call of the tool in this chat, held in the
 *   runner's memory. It goes when the chat goes idle (a background turn
 *   ends, the runner is evicted) or the worker restarts.
 * - `profile`: a permanent, tool-wide rule on the agent, synced to every
 *   worker. It stays until removed under the agent's Approvals tab.
 *
 * A request without the field (an older or replayed frame), a relayed
 * subagent prompt and a model switch are answered once only.
 */
import type { PermissionWorkerTag } from '$lib/api/client';

export type RememberScope = 'none' | 'session' | 'profile';

export const SCOPE_ORDER: readonly RememberScope[] = ['none', 'session', 'profile'];

export interface ScopedRequest {
	tool_name: string;
	remember_scopes?: readonly string[] | null;
	provider_agent?: PermissionWorkerTag | null;
}

function isScope(value: unknown): value is RememberScope {
	return value === 'none' || value === 'session' || value === 'profile';
}

/** The lifetimes this request can be answered with, in display order.
 *  Always starts with `none`. */
export function availableScopes(request: ScopedRequest): RememberScope[] {
	// A model switch and a provider safety check (ModelGuardrail) are
	// single-use: no saved rule ever answers them.
	if (request.tool_name === 'ModelSwitch' || request.tool_name === 'ModelGuardrail') return ['none'];
	if (request.provider_agent?.remote_approval_id) return ['none'];
	const offered = Array.isArray(request.remember_scopes) ? request.remember_scopes.filter(isScope) : [];
	return SCOPE_ORDER.filter((scope) => scope === 'none' || offered.includes(scope));
}

/** True when the prompt should offer a choice at all. */
export function canRemember(request: ScopedRequest): boolean {
	return availableScopes(request).length > 1;
}

/** The `remember` value to send: the chosen lifetime when the request
 *  offers it, otherwise this request only. */
export function rememberFor(choice: RememberScope, request: ScopedRequest): RememberScope {
	return availableScopes(request).includes(choice) ? choice : 'none';
}

/** Option labels. `short` is for the phone dock. */
export function scopeLabel(scope: RememberScope, short = false): string {
	if (scope === 'session') return short ? 'This chat' : 'This chat until it goes idle';
	if (scope === 'profile') return short ? 'Always' : 'Always for this agent';
	return short ? 'Once' : 'Just this once';
}

/** One plain sentence on what the chosen lifetime covers. */
export function scopeHint(scope: RememberScope, tool: string): string {
	if (scope === 'session') {
		return `Covers every ${tool} call in this chat until it goes idle or the worker restarts.`;
	}
	if (scope === 'profile') {
		return `Permanent. Covers every ${tool} call this agent makes, in every chat, on every worker. Remove it under the agent's Approvals tab.`;
	}
	return 'Applies to this request only. You will be asked again next time.';
}

/** Button text that names what the press will save. */
export function decisionLabels(scope: RememberScope): { allow: string; deny: string } {
	if (scope === 'session') return { allow: 'Allow in this chat', deny: 'Deny in this chat' };
	if (scope === 'profile') return { allow: 'Always allow', deny: 'Always deny' };
	return { allow: 'Approve', deny: 'Deny' };
}
