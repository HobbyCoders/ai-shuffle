// Stand-ins for the session store, swarms store, task stop API, session
// navigation and the build-time shell flag. vite.running-tasks.config.js routes
// the production imports here; RunningTasksFixture.svelte drives them.
import { writable, get } from 'svelte/store';
export * from '../src/lib/utils/platform';
// The real modules behind the rest of their names; the fakes below win.
export * from '../src/lib/api/sessions';
export * from '../src/lib/services/session-navigation';
import type { UiPlatform } from '../src/lib/utils/platform';
import type { Task } from '../src/lib/stores/types';
import type { SwarmSnapshot } from '../src/lib/utils/swarm';

export function buildShell(): UiPlatform {
	return new URLSearchParams(location.search).get('shell') === 'mobile' ? 'mobile' : 'desktop';
}

export const activeSession = writable<{ sessionId: string; activeTasks: Task[]; messages: unknown[] }>({
	sessionId: 'fixture-session',
	activeTasks: [],
	messages: []
});

const swarmState = writable<{ bySession: Record<string, Record<string, SwarmSnapshot>>; errors: Record<string, string> }>({
	bySession: {},
	errors: {}
});

export const swarms = {
	subscribe: swarmState.subscribe,
	set(list: SwarmSnapshot[]) {
		swarmState.set({ bySession: { 'fixture-session': Object.fromEntries(list.map((s) => [s.id, s])) }, errors: {} });
	},
	receive() {},
	async hydrate() {},
	refreshKnown() {},
	async cancel(sessionId: string, swarmId: string) {
		await new Promise((r) => setTimeout(r, 400));
		swarmState.update((s) => {
			const current = s.bySession[sessionId]?.[swarmId];
			if (!current) return s;
			const next: SwarmSnapshot = {
				...current,
				status: 'cancelled',
				updated_at: new Date().toISOString(),
				tasks: current.tasks.map((t) => (t.status === 'running' || t.status === 'queued' || t.status === 'blocked' ? { ...t, status: 'cancelled' } : t))
			};
			return { ...s, bySession: { ...s.bySession, [sessionId]: { ...s.bySession[sessionId], [swarmId]: next } } };
		});
		dropTask(swarmId);
	}
};

export const usageTargetDeviceName = writable<string | null>('DesktopWorker');

function dropTask(id: string) {
	activeSession.update((s) => ({ ...s, activeTasks: s.activeTasks.filter((t) => t.id !== id) }));
}

export const stopped: string[] = [];
export async function stopSessionTask(_sessionId: string, taskId: string) {
	stopped.push(taskId);
	await new Promise((r) => setTimeout(r, 500));
	const exists = get(activeSession).activeTasks.some((t) => t.id === taskId);
	dropTask(taskId);
	return { ok: exists };
}

/** Chats streaming on any device: none in the fixture. */
export const streamingSessions = writable<Map<string, unknown>>(new Map());

export const stoppedChats: string[] = [];
export async function stopSession(sessionId: string) {
	stoppedChats.push(sessionId);
	return { status: 'stopped', session_id: sessionId };
}

export const opened: string[] = [];
export function openSession(sessionId: string) {
	opened.push(sessionId);
}
export function openSessionLink(event: MouseEvent, sessionId: string) {
	event.preventDefault();
	openSession(sessionId);
}
export const openSubagentLink = openSessionLink;
