/** Agent names and avatars, loaded once per signed-in owner. */
import { writable } from 'svelte/store';
import { getAgentIdentities, type AgentIdentity } from '$lib/api/agent-identity';
import { sessionOwnerBoundary, sessionOwnerEpoch } from './session-owner';

export const agentIdentities = writable<AgentIdentity[]>([]);
let pending: Promise<void> | null = null;
let revision = 0;
sessionOwnerBoundary.subscribe(() => {
	revision++;
	pending = null;
	agentIdentities.set([]);
});

/** Reload every agent's name and avatar. Concurrent calls share one request. */
export function refreshAgentIdentities(): Promise<void> {
	if (pending) return pending;
	const owner = sessionOwnerEpoch();
	const version = revision;
	const request = getAgentIdentities().then((identities) => {
		if (owner !== sessionOwnerEpoch() || version !== revision) return;
		agentIdentities.set(identities);
	}).finally(() => { if (pending === request) pending = null; });
	pending = request;
	return request;
}
