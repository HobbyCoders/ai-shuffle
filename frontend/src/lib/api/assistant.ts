/** Owner-only assistant controls over the existing unified agents and sessions. */
import { api, type Session } from './client';
import avatarIds from '$lib/components/assistant/avatars/avatars.json';

/** An avatar preset id: a file name in components/assistant/avatars. */
export type AvatarId = string;
/** The preset order, from avatars.json (the backend allowlist is checked against it). */
export const AVATARS: readonly AvatarId[] = avatarIds;
/** `dedicated` marks the agent Assistant setup created for itself. */
export interface AgentIdentity { id: string; name: string; avatar_id: AvatarId; dedicated?: boolean }
export const DEFAULT_ASSISTANT_NAME = 'Assistant';
export const randomAvatar = (): AvatarId => AVATARS[Math.floor(Math.random() * AVATARS.length)];
/**
 * What the assistant knows about its owner. The server detects it from the
 * install (account, Gmail connector, gh CLI, project checkouts); the owner
 * can correct a field, and an empty correction goes back to the detected value.
 */
export interface AssistantIntroduction {
 preferred_name?: string;
 email?: string;
 github_username?: string;
 repository_refs?: string[];
 /** Project names; detected only. */
 projects?: string[];
 /** Only in introductions saved before detection. */
 project_ids?: string[];
 notes?: string;
}
export type IntroductionCorrection = Partial<Pick<AssistantIntroduction, 'preferred_name' | 'email' | 'github_username' | 'repository_refs'>>;
/** Connections the assistant page suggests when this worker has none. */
export type SuggestedConnection = 'gmail' | 'github';
/**
 * Background research has no owner controls: the server runs it whenever an
 * agent is set, its safety guard is up and a source is opted in (`ready`).
 * `enabled` mirrors that. Research limits are server-side only and are not
 * in this response. A run that finds something posts a brief into the home
 * conversation.
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
 /** Detected values with the owner's corrections applied. */
 introduction: AssistantIntroduction;
 introduction_detected?: AssistantIntroduction;
 /** Fields the owner corrected. */
 introduction_corrected?: (keyof AssistantIntroduction)[];
 suggested_connections?: SuggestedConnection[];
 proactive_guard_ready: boolean;
 /** False when the home conversation (and research) lives on another worker. */
 runs_here?: boolean;
 /** The worker that owns the home; null before a home exists or off-network. */
 home_worker?: AssistantWorker | null;
}
export interface AssistantWorker { device_id: string | null; name: string; is_self: boolean; online: boolean }
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
 introduction: IntroductionCorrection;
}>;
/** `detect` asks the server to re-read the owner's details first (briefly). */
export const getAssistant = (detect = false) => api.get<AssistantState>(detect ? '/assistant?detect=true' : '/assistant');
export const configureAssistant = (patch: AssistantPatch) => api.patch<AssistantState>('/assistant', patch);
export const openAssistant = () => api.post<{ session_id: string; home_session_id: string; session?: Session }>('/assistant/open');
export const getAssistantSources = () => api.get<AssistantSource[]>('/assistant/sources');
export const setAssistantSource = (id: string, enabled: boolean) => api.patch<AssistantSource>(`/assistant/sources/${encodeURIComponent(id)}`, { enabled });
export const getAssistantActivity = () => api.get<AssistantActivity[]>('/assistant/activity?limit=50');
export const getAgentIdentities = () => api.get<AgentIdentity[]>('/assistant/agents');
export const updateAgentIdentity = (id: string, patch: { name?: string; avatar_id?: AvatarId }) => api.patch<AgentIdentity>(`/assistant/agents/${encodeURIComponent(id)}/identity`, patch);
/**
 * The owner's limits for the assistant and every job it starts, with today's
 * spend. Spend is in US dollars at API prices, so subscription use is an
 * estimate. The day resets at midnight in the owner's time zone (else UTC).
 */
export interface AssistantLimits {
 spend_limit_enabled: boolean;
 daily_spend_limit_usd: number;
 job_limit_enabled: boolean;
 max_job_minutes: number;
 spent_today_usd: number;
 day: string;
 timezone: string;
 resets_at: string;
 limit_reached: boolean;
 warning: boolean;
}
export type AssistantLimitsPatch = Partial<Pick<AssistantLimits, 'spend_limit_enabled' | 'daily_spend_limit_usd' | 'job_limit_enabled' | 'max_job_minutes'>> & {
 /** Required with spend_limit_enabled: false. */
 confirm_spend_limit_off?: boolean;
};
export const getAssistantLimits = () => api.get<AssistantLimits>('/assistant/limits');
export const updateAssistantLimits = (patch: AssistantLimitsPatch) => api.patch<AssistantLimits>('/assistant/limits', patch);
export function assistantError(error: unknown): string {
 const detail = (error as { detail?: unknown })?.detail;
 return typeof detail === 'string' ? detail : error instanceof Error ? error.message : 'Could not save this change. Please try again.';
}
