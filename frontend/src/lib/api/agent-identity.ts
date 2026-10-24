/**
 * Agent names and avatars. Every agent carries one; the new-session agent
 * card, the agent editor and the agent lists all read them from here.
 *
 * Current clients use `/owner/agents`; the server keeps the historical prefix
 * as a thin alias for older installed clients.
 */
import { api } from './client';
import avatarIds from '$lib/components/agents/avatars/avatars.json';

/** An avatar preset id: a file name in components/agents/avatars. */
export type AvatarId = string;
/** The preset order, from avatars.json (the backend allowlist is checked against it). */
export const AVATARS: readonly AvatarId[] = avatarIds;
export interface AgentIdentity { id: string; name: string; avatar_id: AvatarId; dedicated?: boolean }

export const getAgentIdentities = () => api.get<AgentIdentity[]>('/owner/agents');
export const updateAgentIdentity = (id: string, patch: { name?: string; avatar_id?: AvatarId }) =>
	api.patch<AgentIdentity>(`/owner/agents/${encodeURIComponent(id)}/identity`, patch);

/** The server's own words for a failed save, else a plain fallback. */
export function apiErrorText(error: unknown): string {
	const detail = (error as { detail?: unknown })?.detail;
	return typeof detail === 'string' ? detail : error instanceof Error ? error.message : 'Could not save this change. Please try again.';
}
