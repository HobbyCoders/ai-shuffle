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

// ─── Per-tab active-session slot (sessionStorage-scoped) ─────────────────────
//
// `deviceId` (localStorage) is SHARED by every tab in the same browser, so two
// tabs both persisting their active session under the server device key
// `active_session:<deviceId>` clobber each other — reloading one tab restores
// the OTHER tab's session.
//
// The fix keeps the per-tab slot CLIENT-side: sessionStorage is already scoped
// per-tab (and per-tab-duplicate) and dies with the tab, so it needs no id in
// the key and creates zero server rows. Each tab writes its active session to
// this single sessionStorage key AND (unchanged) to the server device key; on
// load it prefers this tab-local value, falling back to the server device key
// for a brand-new tab / new device. `deviceId` stays untouched for cross-device
// features.

/** sessionStorage key holding THIS tab's serialized active session (JSON). */
export const TAB_ACTIVE_SESSION_KEY = 'ai-shuffle-active-session';
