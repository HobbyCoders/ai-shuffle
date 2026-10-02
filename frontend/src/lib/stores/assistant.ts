/** Presentation state only. The backend owns configuration, admission and identity. */
import { writable } from 'svelte/store';
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
