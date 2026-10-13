/** Presentation state only. The backend owns configuration, admission and identity. */
import { get, writable } from 'svelte/store';
import { getAssistant, getAgentIdentities, type AssistantState, type AgentIdentity } from '$lib/api/assistant';
import { sessionOwnerBoundary, sessionOwnerEpoch } from './session-owner';

export const assistantState = writable<AssistantState | null>(null);
export const agentIdentities = writable<AgentIdentity[]>([]);
let pending: Promise<void> | null = null;
let revision = 0;
sessionOwnerBoundary.subscribe(() => {
 revision++;
 pending = null;
 assistantState.set(null);
 agentIdentities.set([]);
});
/**
 * True when `sessionId` is the assistant's home conversation. The chat view's
 * "assistant mode" is derived from this one check, in one place (MainPanel),
 * and passed down as a flag.
 */
export function isAssistantHome(sessionId: string | null | undefined, state: AssistantState | null | undefined): boolean {
 return !!sessionId && !!state?.home_session_id && state.home_session_id === sessionId;
}
/**
 * Whether a bound chat is the built-in assistant home, for actions that must
 * fail closed (Rename / Fork / Delete). `isAdmin` is null while the auth status
 * is unknown. Only an admin has an assistant, so a non-admin's chat is ordinary
 * without the assistant state; an admin's stays 'unknown' until that state
 * loads (and while a load has failed).
 */
export type ChatIdentity = 'unknown' | 'assistant' | 'ordinary';
export function chatIdentity(
 sessionId: string | null | undefined,
 isAdmin: boolean | null,
 state: AssistantState | null | undefined
): ChatIdentity {
 if (isAdmin === null) return 'unknown';
 if (!isAdmin) return 'ordinary';
 if (!state) return 'unknown';
 return isAssistantHome(sessionId, state) ? 'assistant' : 'ordinary';
}
/**
 * The effort a send from `sessionId` carries. The assistant's home always runs
 * at its model's default, so it sends none, whatever the composer remembers.
 */
export function effortToSend(sessionId: string | null | undefined, effort: string | null | undefined): string | null {
 return isAssistantHome(sessionId, get(assistantState)) ? null : effort || null;
}
export function acceptAssistant(state: AssistantState) {
 revision++;
 assistantState.set(state);
}
/** `detect` re-reads the owner's details on the server before answering. */
export function refreshAssistant(detect = false): Promise<void> {
 if (pending) return pending;
 const owner = sessionOwnerEpoch();
 const version = revision;
 const request = Promise.all([getAssistant(detect), getAgentIdentities()]).then(([state, identities]) => {
  if (owner !== sessionOwnerEpoch() || version !== revision) return;
  assistantState.set(state);
  agentIdentities.set(identities);
 }).finally(() => { if (pending === request) pending = null; });
 pending = request;
 return request;
}
