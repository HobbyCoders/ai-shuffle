/**
 * Computer-use API client.
 *
 * Backs the capability toggle (status → enable/grey-out) and the global
 * emergency "Stop controlling my computer" button (stop / resume).
 */
import { api } from './client';

export interface ComputerUseStatus {
	/** Whether this host can run computer-use (false in Docker/headless). */
	available: boolean;
	/** "ok" when available; otherwise why not: docker, no-display, missing-deps, ... */
	reason: string;
	/** Whether the emergency stop is currently engaged. */
	stopped: boolean;
	/** True when the agent ran a screen action within the recent window. */
	active: boolean;
	/** Which monitor it's acting on (when active). */
	display: number | null;
	/** Human label for the active display, e.g. "Display 1 · 3440×1440 (primary)". */
	display_label: string | null;
}

export async function getComputerUseStatus(): Promise<ComputerUseStatus> {
	return api.get<ComputerUseStatus>('/computer-use/status');
}

export async function stopComputerUse(): Promise<{ stopped: boolean }> {
	return api.post<{ stopped: boolean }>('/computer-use/stop');
}

export async function resumeComputerUse(): Promise<{ stopped: boolean }> {
	return api.post<{ stopped: boolean }>('/computer-use/resume');
}
