/** Typed machine operations. No commands, URLs, credentials or sign-in writes. */
import { api } from './client';

export type WorkerAction =
	| { operation: 'claude-install' | 'claude-update' }
	| { operation: 'claude-keep-installed'; keep_installed: boolean }
	| { operation: 'release-channel'; channel: 'stable' | 'nightly' }
	| { operation: 'always-on'; always_on: boolean | null };

export interface MachineStatus {
	claude: {
		installed: boolean;
		version: string | null;
		keep_installed: boolean;
		installed_by_app: boolean;
		can_install: boolean;
		signed_in: boolean | null;
		email: string | null;
		plan: string | null;
		status_available: boolean;
		job: { kind: 'install' | 'update' | 'remove'; state: 'running' | 'succeeded' | 'failed' } | null;
	} | null;
	github_signed_in: boolean | null;
	release_channel: { channel: 'stable' | 'nightly'; editable: boolean; restart_required: boolean } | null;
	/** Reserved extension point; sign-in remains CLI-owned and status-only. */
	claude_sign_in_supported: false;
}

export interface WorkerResult {
	device_id: string;
	device_name: string;
	outcome: 'ok' | 'accepted' | 'offline' | 'unsupported' | 'refused' | 'uncertain';
	reason: string;
	status: MachineStatus | null;
}
export interface WorkerMachine extends WorkerResult {
	is_self: boolean;
	online: boolean;
	availability: { editable: boolean; always_on: boolean | null; always_on_override: boolean | null };
}
export const getWorkerSettings = () => api.get<{ workers: WorkerMachine[] }>('/settings/worker-settings');
export const actOnWorkers = (target: string, action: WorkerAction) =>
	api.post<{ request_id: string; workers: WorkerResult[] }>('/settings/worker-settings/action', { target, action });
