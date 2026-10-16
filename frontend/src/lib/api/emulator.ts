/** REST side of the worker's Android emulator (`/api/v1/emulator`). */
import { api } from './client';
import { apiBase, withAuth } from './runtime';
import { parseStatus, type EmulatorStatus } from '$lib/emulator/protocol';

const dev = (id: string) => `/emulator/devices/${encodeURIComponent(id)}`;

export async function fetchEmulatorStatus(signal?: AbortSignal): Promise<EmulatorStatus> {
	return parseStatus(await api.get<unknown>('/emulator/status', { signal }));
}

/** Starts the SDK download (202). Progress shows up in the status. */
export async function setupEmulator(): Promise<void> {
	await api.post<unknown>('/emulator/setup', { accept_license: true });
}

export async function startDevice(id: string): Promise<void> {
	await api.post<unknown>(`${dev(id)}/start`);
}

export async function stopDevice(id: string): Promise<void> {
	await api.post<unknown>(`${dev(id)}/stop`);
}

export async function installApk(id: string, file: File, signal?: AbortSignal): Promise<{ package: string }> {
	const body = new FormData();
	body.append('apk', file);
	const response = await fetch(`${apiBase()}${dev(id)}/install`, withAuth({ method: 'POST', body, signal }));
	const data = await response.json().catch(() => null);
	if (!response.ok) {
		const detail = typeof data?.detail === 'string' ? data.detail : `Install failed (${response.status})`;
		throw { detail, status: response.status };
	}
	return { package: typeof data?.package === 'string' ? data.package : '' };
}

/** Best-effort text out of an ApiClient rejection. */
export function errorText(error: unknown, fallback: string): string {
	if (error && typeof error === 'object') {
		const detail = (error as { detail?: unknown }).detail;
		if (typeof detail === 'string' && detail) return detail;
		if (detail && typeof detail === 'object' && typeof (detail as { message?: unknown }).message === 'string')
			return (detail as { message: string }).message;
		if (error instanceof Error && error.message) return error.message;
	}
	return fallback;
}

export function errorStatus(error: unknown): number | null {
	const status = error && typeof error === 'object' ? (error as { status?: unknown }).status : null;
	return typeof status === 'number' ? status : null;
}
