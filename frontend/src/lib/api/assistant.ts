/** Owner-only assistant controls over the existing unified agents and sessions. */
import { api } from './client';

export const AVATARS = [
 'robot', 'spark', 'orbit', 'sprout', 'pixel', 'owl', 'fox', 'cat',
 'bear', 'ghost', 'cloud', 'moon', 'sun', 'bolt', 'gem', 'drop'
] as const;
export type AvatarId = typeof AVATARS[number];
/** `dedicated` marks the agent Assistant setup created for itself. */
export interface AgentIdentity { id: string; name: string; avatar_id: AvatarId; dedicated?: boolean }
export const DEFAULT_ASSISTANT_NAME = 'Assistant';
export const randomAvatar = (): AvatarId => AVATARS[Math.floor(Math.random() * AVATARS.length)];
export interface AssistantIntroduction {
 preferred_name?: string;
 github_username?: string;
 repository_refs?: string[];
 project_ids?: string[];
 notes?: string;
}
/**
 * Background research has no owner controls: the server runs it whenever an
 * agent is set, its safety guard is up and a source is opted in (`ready`).
 * `enabled` mirrors that; the server still returns legacy limit and usage
 * fields for older clients, and this UI ignores them.
 */
export interface AssistantState {
 agent_id: string | null;
 home_session_id: string | null;
 configured: boolean;
 enabled: boolean;
 status: 'unconfigured' | 'agent_unavailable' | 'guard_unavailable' | 'no_sources' | 'ready';
 /** Opted-in sources research may read. */
 research_sources: number;
 agent: AgentIdentity | null;
 assistant_name: string | null;
 avatar_id: AvatarId;
 avatar_presets: AvatarId[];
 introduction: AssistantIntroduction;
 proactive_guard_ready: boolean;
}
export interface AssistantSource {
 id: string; connector_id: string; name: string; type: string;
 enabled: boolean; available: boolean; status: string; trigger_ids: string[];
 observation_mode: string; last_observed_at: string | null; last_error: string | null;
}
export interface AssistantActivity {
 id: string; kind: string; status: string; session_id: string | null;
 session_title: string | null; session_status: string | null;
 agent_name: string | null; avatar_id: AvatarId;
 source_ids: string[]; event_ids: string[]; cost_usd: number;
 error: string | null; created_at: string; ended_at: string | null;
}
export type AssistantPatch = Partial<{
 agent_id: string; create_agent: boolean; assistant_name: string; avatar_id: AvatarId;
}>;
export const getAssistant = () => api.get<AssistantState>('/assistant');
export const configureAssistant = (patch: AssistantPatch) => api.patch<AssistantState>('/assistant', patch);
export const openAssistant = () => api.post<{ session_id: string; home_session_id: string }>('/assistant/open');
export const getAssistantSources = () => api.get<AssistantSource[]>('/assistant/sources');
export const setAssistantSource = (id: string, enabled: boolean) => api.patch<AssistantSource>(`/assistant/sources/${encodeURIComponent(id)}`, { enabled });
export const getAssistantActivity = () => api.get<AssistantActivity[]>('/assistant/activity?limit=50');
export const getAgentIdentities = () => api.get<AgentIdentity[]>('/assistant/agents');
export const updateAgentIdentity = (id: string, patch: { name?: string; avatar_id?: AvatarId }) => api.patch<AgentIdentity>(`/assistant/agents/${encodeURIComponent(id)}/identity`, patch);
export function assistantError(error: unknown): string {
 const detail = (error as { detail?: unknown })?.detail;
 return typeof detail === 'string' ? detail : error instanceof Error ? error.message : 'Could not save this change. Please try again.';
}
