// Stand-ins for the app-wide stores and services AssistantHome reaches beyond
// its own API. vite.assistant-setup.config.js routes those production imports
// here; /api/v1/assistant itself is answered by the verifier's fake backend.
import { writable } from 'svelte/store';

export const profiles = writable([
	{ id: 'researcher', name: 'Researcher', is_builtin: false, config: {} },
	{ id: 'reviewer', name: 'Reviewer', is_builtin: false, config: {} }
]);
export const projects = writable([]);
export const sessions = writable([]);
export const appData = { async loadProfiles() {}, async loadSessions() {} };
export const layout = {
	openFeaturePage(page: string, id?: string) {
		(window as unknown as { assistantFixture: { opened: unknown[] } }).assistantFixture.opened.push([page, id]);
	}
};
export async function isVoiceEnabled() {
	return false;
}
// Reached through the session stores (voiceExecutionContext); never called here.
export async function updateVoiceContext() {
	return {};
}
export async function startCallForSession() {}
// Reached through the session stores (message-sender); no call runs here.
export function isCallActiveFor() {
	return false;
}
export async function sendVoiceText() {}
export function openSession(id: string) {
	(window as unknown as { assistantFixture: { opened: unknown[] } }).assistantFixture.opened.push(['session', id]);
}
