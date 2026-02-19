/**
 * Device identification store for cross-device synchronization.
 * Generates and persists a unique device ID for each browser/device.
 *
 * Migrated to Svelte 5 runes.
 */

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

	let id = localStorage.getItem(DEVICE_ID_KEY);
	if (!id) {
		id = generateUUID();
		localStorage.setItem(DEVICE_ID_KEY, id);
	}
	return id;
}

// Module-level reactive state
const _deviceId = $state(getDeviceId());

/**
 * Reactive device ID store (Svelte 5 runes).
 *
 * Usage in .svelte files:
 *   import { deviceId } from '$lib/stores/device.svelte';
 *   // Access: deviceId.current
 *
 * Also implements subscribe() for backward compatibility with $deviceId syntax.
 */
export const deviceId = {
	get current() {
		return _deviceId;
	},
	subscribe(fn: (value: string) => void) {
		fn(_deviceId);
		// Value never changes after init, so no cleanup needed
		return () => {};
	}
};

/**
 * Get the device ID directly (for use outside of Svelte components)
 */
export function getDeviceIdSync(): string {
	return _deviceId;
}
