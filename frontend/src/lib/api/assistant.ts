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
/** Fixed server-side (RESEARCH_LIMITS in app/db/assistant.py); read-only here. */
export interface AssistantLimits {
 max_runs_per_day: number;
 max_concurrent_runs: number;
 max_turns: number;
 timeout_seconds: number;
 max_cost_per_run: number;
 max_cost_per_day: number;
}
export interface AssistantState extends AssistantLimits {
 agent_id: string | null;
 home_session_id: string | null;
 configured: boolean;
 enabled: boolean;
 paused: boolean;
 status: 'unconfigured' | 'agent_unavailable' | 'disabled' | 'paused' | 'guard_unavailable' | 'ready';
 agent: AgentIdentity | null;
 assistant_name: string | null;
 avatar_id: AvatarId;
 avatar_presets: AvatarId[];
 introduction: AssistantIntroduction;
 proactive_guard_ready: boolean;
 cost_accounting: string;
 usage: { runs_today: number; reported_cost_today: number; in_flight_runs: number };
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
 enabled: boolean; paused: boolean;
}>;
export const getAssistant = () => api.get<AssistantState>('/assistant');
export const configureAssistant = (patch: AssistantPatch) => api.patch<AssistantState>('/assistant', patch);
export const openAssistant = () => api.post<{ session_id: string; home_session_id: string }>('/assistant/open');
export const pauseAssistant = () => api.post<AssistantState>('/assistant/pause');
export const resumeAssistant = () => api.post<AssistantState>('/assistant/resume');
export const getAssistantSources = () => api.get<AssistantSource[]>('/assistant/sources');
export const setAssistantSource = (id: string, enabled: boolean) => api.patch<AssistantSource>(`/assistant/sources/${encodeURIComponent(id)}`, { enabled });
export const getAssistantActivity = () => api.get<AssistantActivity[]>('/assistant/activity?limit=50');
export const getAgentIdentities = () => api.get<AgentIdentity[]>('/assistant/agents');
export const updateAgentIdentity = (id: string, patch: { name?: string; avatar_id?: AvatarId }) => api.patch<AgentIdentity>(`/assistant/agents/${encodeURIComponent(id)}/identity`, patch);
export function assistantError(error: unknown): string {
 const detail = (error as { detail?: unknown })?.detail;
 return typeof detail === 'string' ? detail : error instanceof Error ? error.message : 'Could not save this change. Please try again.';
}
