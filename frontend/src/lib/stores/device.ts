/**
 * Device identification store for cross-device synchronization.
 * Generates and persists a unique device ID for each browser/device.
 */

import { readable } from 'svelte/store';

const DEVICE_ID_KEY = 'ai-shuffle-device-id';

/**
 * Generate a UUID v4
 */
function generateUUID(): string {
	if (typeof crypto !== 'undefined' && crypto.randomUUID) {
		return crypto.randomUUID();
	}
	// Fallback for older browsers
	return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
		const r = (Math.random() * 16) | 0;
		const v = c === 'x' ? r : (r & 0x3) | 0x8;
		return v.toString(16);
	});
}

/**
 * Get or create a persistent device ID
 */
function getDeviceId(): string {
	if (typeof localStorage === 'undefined') {
		// SSR or no localStorage - generate temporary ID
		return generateUUID();
	}

	let deviceId = localStorage.getItem(DEVICE_ID_KEY);
	if (!deviceId) {
		deviceId = generateUUID();
		localStorage.setItem(DEVICE_ID_KEY, deviceId);
	}
	return deviceId;
}

/**
 * Readable store containing the unique device ID.
 * This ID persists across sessions via localStorage.
 */
export const deviceId = readable<string>(getDeviceId());

/**
 * Get the device ID directly (for use outside of Svelte components)
 */
export function getDeviceIdSync(): string {
	return getDeviceId();
}
