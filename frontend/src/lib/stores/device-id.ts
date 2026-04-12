/**
 * Device ID — stable per-browser identifier used to isolate per-device state.
 *
 * Stored in localStorage so each browser/device gets a unique ID.
 * Used to scope preferences (active_session, selection) so that
 * multiple devices sharing the same DB don't clobber each other's state.
 */

const CANONICAL_KEY = 'ai-shuffle-device-id';
const LEGACY_KEY = 'aihub_device_id';

let _cached: string | null = null;

export function getOrCreateDeviceId(): string {
	if (_cached) return _cached;

	if (typeof window === 'undefined') {
		return `device-${Date.now()}-${Math.random().toString(36).substring(2, 11)}`;
	}

	let deviceId = localStorage.getItem(CANONICAL_KEY);
	if (deviceId) {
		_cached = deviceId;
		return deviceId;
	}

	const legacyId = localStorage.getItem(LEGACY_KEY);
	if (legacyId) {
		localStorage.setItem(CANONICAL_KEY, legacyId);
		localStorage.removeItem(LEGACY_KEY);
		_cached = legacyId;
		return legacyId;
	}

	deviceId = `device-${Date.now()}-${Math.random().toString(36).substring(2, 11)}`;
	localStorage.setItem(CANONICAL_KEY, deviceId);
	_cached = deviceId;
	return deviceId;
}
