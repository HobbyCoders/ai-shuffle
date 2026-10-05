/**
 * Which worker the phone app is connected to, and how to connect it to
 * another one (issue #1816, "Connected to" bar).
 *
 * This is the APP's connection — the worker whose UI and chats this phone
 * shows — and is kept apart from a chat's "Runs on" worker, which the chat
 * controls move.
 *
 * Switching is the app's own job: the Android app owns the pairing and
 * reopens chat on another paired worker. A page cannot reach that without a
 * native capability, so the switch is offered three ways, best first:
 *  - `worker-switch` capability: navigate the phone UI to
 *    `/mobile/?connect=<worker origin>`; the app intercepts it, finds that
 *    paired worker and reopens chat there (its existing switch path).
 *  - a plain browser (no app runtime): open that worker's own /mobile/.
 *  - an app without the capability: say where the switch lives in the app.
 */
import type { Install } from '$lib/api/installs';
import type { Session } from '$lib/api/sessions';

/** Native capability name an app build adds once it handles `?connect=`. */
export const WORKER_SWITCH_CAPABILITY = 'worker-switch';

export interface ConnectionWorker {
	id: string;
	name: string;
	online: boolean;
	current: boolean;
	running: number;
	baseUrl: string | null;
}

/** A session counts as running while a turn or background work is live. */
export function sessionRunning(session: Session, streaming: ReadonlySet<string>): boolean {
	if (streaming.has(session.id)) return true;
	if (session.work_started_at) return true;
	if (session.has_running_tasks) return true;
	return session.remote_runtime_state === 'working' || session.remote_runtime_state === 'parked';
}

function ownerKeys(install: Install): string[] {
	return [install.id, install.device_id, install.legacy_instance_id].filter((v): v is string => !!v);
}

/** The owner's workers, current first, with their running-chat counts. */
export function connectionWorkers(installs: readonly Install[], sessions: readonly Session[], streaming: ReadonlySet<string>): ConnectionWorker[] {
	const usable = installs.filter((i) => i.approved !== false && !i.revoked_at);
	const rows = usable.map((install) => {
		const keys = ownerKeys(install);
		const running = sessions.filter((s) => {
			if (s.parent_session_id || !sessionRunning(s, streaming)) return false;
			if (install.is_self) return !s.is_remote;
			return keys.includes(s.owner_device_id ?? '') || keys.includes(s.instance_id ?? '');
		}).length;
		return {
			id: install.id,
			name: install.device_name || 'Worker',
			online: install.is_self ? true : install.online,
			current: !!install.is_self,
			running,
			baseUrl: install.base_url
		};
	});
	return rows.sort((a, b) => Number(b.current) - Number(a.current) || Number(b.online) - Number(a.online) || a.name.localeCompare(b.name));
}

export type SwitchRoute =
	| { kind: 'current' }
	| { kind: 'app'; url: string }
	| { kind: 'browser'; url: string }
	| { kind: 'unavailable'; reason: string };

function origin(url: string | null): string | null {
	if (!url) return null;
	try { return new URL(url).origin; } catch { return null; }
}

/** How a tap on `worker` connects the app to it. */
export function switchRoute(
	worker: ConnectionWorker,
	env: { inApp: boolean; canSwitch: boolean; pageUrl: string }
): SwitchRoute {
	if (worker.current) return { kind: 'current' };
	const target = origin(worker.baseUrl);
	if (!target) return { kind: 'unavailable', reason: `${worker.name} has no address to connect to` };
	if (!worker.online) return { kind: 'unavailable', reason: `${worker.name} is offline` };
	if (env.inApp && env.canSwitch) {
		const url = new URL(env.pageUrl);
		const base = url.pathname.startsWith('/mobile') ? '/mobile/' : '/';
		return { kind: 'app', url: `${url.origin}${base}?connect=${encodeURIComponent(target)}` };
	}
	if (!env.inApp) return { kind: 'browser', url: `${target}/mobile/` };
	return { kind: 'unavailable', reason: 'Switch in the app menu: ⋮, then Workers' };
}
