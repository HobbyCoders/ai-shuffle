/**
 * Computer-use API client.
 *
 * Backs the global toggle (Settings > Security) and the live status the
 * UI's indicators consume. The host stop endpoint releases active desktop
 * control without requiring the user to find the chat that owns it.
 */
import { api } from './client';

export interface ComputerUseStatus {
	/** Whether this host can run computer-use (false in Docker/headless). */
	available: boolean;
	/**
	 * "ok" when available; otherwise why not: docker, no-display, wayland,
	 * missing-deps, missing-system-packages, x11-unreachable, x11-no-xtest, ...
	 */
	reason: string;
	/** When unavailable: what is wrong on this host and how to fix it. */
	detail?: string | null;
	/** Packages to install, when that is why it is unavailable. */
	missing?: string[];
	/** Whether the global toggle is currently on. */
	enabled: boolean;
	/** True until the owning chat releases control, finishes, or is stopped. */
	active: boolean;
	/** Which monitor it's acting on (when active). */
	display: number | null;
	/** Human label for the active display, e.g. "Display 1 · 3440×1440 (primary)". */
	display_label: string | null;
	action?: string | null;
	busy?: boolean;
	cursor?: [number, number] | null;
	geometry?: { left: number; top: number; width: number; height: number } | null;
	seconds_since?: number | null;
	stop_reason?: string | null;
	escape_available?: boolean;
	overlay_available?: boolean;
	/** "dedicated_desktop" when only agents use this computer (skips the first-use prompt). */
	input_mode?: ComputerUseInputMode;
}

export type ComputerUseInputMode = 'shared_desktop' | 'dedicated_desktop';

export async function stopComputerUse(): Promise<{ stopped: number }> {
	return api.post<{ stopped: number }>('/computer-use/stop', {});
}

export async function getComputerUseStatus(): Promise<ComputerUseStatus> {
	return api.get<ComputerUseStatus>('/computer-use/status');
}

export async function setComputerUseEnabled(enabled: boolean): Promise<{ enabled: boolean }> {
	return api.put<{ enabled: boolean }>('/computer-use/enabled', { enabled });
}

export async function setComputerUseInputMode(
	input_mode: ComputerUseInputMode
): Promise<{ input_mode: ComputerUseInputMode }> {
	return api.put<{ input_mode: ComputerUseInputMode }>('/computer-use/input-mode', { input_mode });
}
