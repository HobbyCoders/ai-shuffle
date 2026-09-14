/** Browser-local default for new chats; existing sessions keep their own owner.
 * Do not sync this preference: `null` means this browser's local worker.
 * Availability is checked by the routing API, never by silently falling back.
 */
import { browser } from '$app/environment';
import type { DeviceIntent } from './types';

const STORAGE_KEY = 'aihub_workerSelection';

export function getPersistedDeviceIntent(): DeviceIntent | null {
	try {
		if (!browser) return null;
		const value: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null');
		if (!value || typeof value !== 'object') return null;
		const { id, name } = value as Partial<DeviceIntent>;
		return typeof id === 'string' && id.trim() && typeof name === 'string' && name.trim()
			? { id, name } : null;
	} catch {
		return null;
	}
}

export function setPersistedDeviceIntent(intent: DeviceIntent | null): void {
	try {
		if (browser) localStorage.setItem(STORAGE_KEY, JSON.stringify(intent));
	} catch {
		// Blocked/full storage must not prevent the current selection.
	}
}
