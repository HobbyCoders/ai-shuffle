import type { Install } from '$lib/api/installs';

/**
 * A worker's name comes from its host name unless someone renamed it, and a
 * Docker container's default host name is its 12-character container ID. That
 * ID is not a name, so no worker control should ever show it.
 */
const CONTAINER_ID = /^[0-9a-f]{12}$/;

export function workerDisplayName(name: string | null | undefined, fallback = 'Worker'): string {
	const clean = (name ?? '').trim();
	if (!clean) return fallback;
	if (CONTAINER_ID.test(clean)) return fallback === 'Worker' ? `Worker ${clean.slice(0, 4)}` : fallback;
	return clean;
}

/** What this worker is called, the same on new chat and on an open chat. */
export function selfWorkerName(installs: readonly Install[] | null | undefined): string {
	return workerDisplayName(installs?.find((i) => i.is_self)?.device_name, 'Local');
}
