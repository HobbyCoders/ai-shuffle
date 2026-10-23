/**
 * Agent names and avatars. Every agent carries one; the new-session agent
 * card, the agent editor and the agent lists all read them from here.
 *
 * The routes still live under `/assistant/agents` on the server; that prefix
 * is all that remains of the retired personal assistant here (#371 step 5).
 */
import { api } from './client';
import avatarIds from '$lib/components/agents/avatars/avatars.json';

/** An avatar preset id: a file name in components/agents/avatars. */
export type AvatarId = string;
/** The preset order, from avatars.json (the backend allowlist is checked against it). */
export const AVATARS: readonly AvatarId[] = avatarIds;
export interface AgentIdentity { id: string; name: string; avatar_id: AvatarId; dedicated?: boolean }

export const getAgentIdentities = () => api.get<AgentIdentity[]>('/assistant/agents');
export const updateAgentIdentity = (id: string, patch: { name?: string; avatar_id?: AvatarId }) =>
	api.patch<AgentIdentity>(`/assistant/agents/${encodeURIComponent(id)}/identity`, patch);

/** The server's own words for a failed save, else a plain fallback. */
export function apiErrorText(error: unknown): string {
	const detail = (error as { detail?: unknown })?.detail;
	return typeof detail === 'string' ? detail : error instanceof Error ? error.message : 'Could not save this change. Please try again.';
}
