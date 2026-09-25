/**
 * Device performance profile — one flag, read by CSS.
 *
 * PERFORMANCE_REVIEW §3.2 R13 asks for an opaque fallback for the app's glass
 * surfaces "under prefers-reduced-transparency / a low-end heuristic, driven by
 * CSS custom properties so it is one switch". The media query half lives in
 * app.css; this is the heuristic half: it stamps `data-fx="low"` on <html>,
 * which the same custom-property block keys off.
 *
 * The heuristic is deliberately conservative — it only fires on hints the
 * browser actually gives us, and an unknown machine is treated as capable, so
 * the design keeps its character everywhere it can afford to.
 */

/** Cores at or below this: treat as low-end. */
export const LOW_END_CORES = 4;
/** GB of RAM at or below this (Chromium's deviceMemory hint): treat as low-end. */
export const LOW_END_MEMORY_GB = 4;

export interface DeviceHints {
	hardwareConcurrency?: number | null;
	deviceMemoryGb?: number | null;
	saveData?: boolean;
}

/** Pure predicate so the policy can be tested without a browser. */
export function isLowEndDevice(hints: DeviceHints): boolean {
	const { hardwareConcurrency, deviceMemoryGb, saveData } = hints;
	if (saveData) return true;
	if (typeof hardwareConcurrency === 'number' && hardwareConcurrency > 0) {
		if (hardwareConcurrency <= LOW_END_CORES) return true;
	}
	if (typeof deviceMemoryGb === 'number' && deviceMemoryGb > 0) {
		if (deviceMemoryGb <= LOW_END_MEMORY_GB) return true;
	}
	return false;
}

/** Read whatever hints this browser exposes. */
function readDeviceHints(): DeviceHints {
	if (typeof navigator === 'undefined') return {};
	const nav = navigator as Navigator & { deviceMemory?: number; connection?: { saveData?: boolean } };
	return {
		hardwareConcurrency: typeof nav.hardwareConcurrency === 'number' ? nav.hardwareConcurrency : null,
		deviceMemoryGb: typeof nav.deviceMemory === 'number' ? nav.deviceMemory : null,
		saveData: nav.connection?.saveData === true
	};
}

let applied = false;

/**
 * Stamp the profile onto <html> once per document. Idempotent and safe to call
 * from any component's mount — the first caller wins and later calls are free.
 */
export function applyPerformanceProfile(): void {
	if (applied || typeof document === 'undefined') return;
	applied = true;
	if (isLowEndDevice(readDeviceHints())) {
		document.documentElement.dataset.fx = 'low';
	}
}
