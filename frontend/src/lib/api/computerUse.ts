/**
 * Computer-use API client.
 *
 * Backs the global toggle (Settings → Chat & Input) and the live status the
 * UI's indicators consume. Halting computer-use for a session is the chat
 * Stop button's job (the WS `stop` frame); there is no separate stop/resume
 * endpoint anymore.
 */
import { api } from './client';

export interface ComputerUseStatus {
	/** Whether this host can run computer-use (false in Docker/headless). */
	available: boolean;
	/** "ok" when available; otherwise why not: docker, no-display, missing-deps, ... */
	reason: string;
	/** Whether the global toggle is currently on. */
	enabled: boolean;
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

export async function getComputerUseEnabled(): Promise<{ enabled: boolean }> {
	return api.get<{ enabled: boolean }>('/computer-use/enabled');
}

export async function setComputerUseEnabled(enabled: boolean): Promise<{ enabled: boolean }> {
	return api.put<{ enabled: boolean }>('/computer-use/enabled', { enabled });
}
