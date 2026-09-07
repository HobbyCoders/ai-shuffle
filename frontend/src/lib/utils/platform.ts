/**
 * UI platform detection.
 *
 * The frontend ships one bundle but two shells: the desktop UI (every web
 * browser, at any window width) and the mobile UI (the Android app's
 * embedded WebView). Which shell renders is a *platform* decision made once
 * at boot, never a viewport-width decision. Narrow web windows keep the
 * desktop shell and only resize.
 *
 * Detection order:
 *   1. `?ui=mobile` / `?ui=desktop` in the URL. Persisted to localStorage so
 *      a dev can load the mobile shell in a desktop browser and keep it across
 *      reloads. `?ui=auto` clears the override.
 *   2. A persisted override from a previous `?ui=` visit.
 *   3. The Android app's user-agent token (`AIShuffleAndroid/<version>`),
 *      appended by ChatActivity to the WebView's default user agent.
 *   4. Otherwise desktop.
 *
 * The same logic runs inline in `app.html` before first paint (as ES5, so it
 * cannot import this module) and stamps `<html data-platform="…">`. Keep the
 * two in sync; `platform.test.ts` covers this module and the inline script
 * text.
 */

export type UiPlatform = 'mobile' | 'desktop';

/** Product token the Android app appends to the WebView user agent. */
export const ANDROID_UA_PRODUCT = 'AIShuffleAndroid';

/** URL query key for a manual shell override (`?ui=mobile|desktop|auto`). */
export const UI_PLATFORM_QUERY_KEY = 'ui';

/** localStorage key holding a persisted `?ui=` override. */
export const UI_PLATFORM_STORAGE_KEY = 'ai-shuffle-ui-platform';

/** Attribute on `<html>` carrying the resolved platform for CSS. */
export const UI_PLATFORM_ATTRIBUTE = 'data-platform';

const ANDROID_UA_PATTERN = new RegExp(`\\b${ANDROID_UA_PRODUCT}/`);

export function isUiPlatform(value: unknown): value is UiPlatform {
	return value === 'mobile' || value === 'desktop';
}

export interface DetectUiPlatformInput {
	userAgent?: string | null;
	/** `location.search`, with or without the leading `?`. */
	search?: string | null;
	/** Previously persisted override (`localStorage[UI_PLATFORM_STORAGE_KEY]`). */
	stored?: string | null;
}

export interface DetectUiPlatformResult {
	platform: UiPlatform;
	/** Override to persist, `null` to clear one, `undefined` to leave storage alone. */
	persist?: UiPlatform | null;
}

/** Pure detection; see the module comment for precedence. */
export function detectUiPlatform(input: DetectUiPlatformInput): DetectUiPlatformResult {
	const query = readQueryOverride(input.search);
	if (query === 'auto') {
		return { platform: fromUserAgent(input.userAgent), persist: null };
	}
	if (isUiPlatform(query)) {
		return { platform: query, persist: query };
	}
	if (isUiPlatform(input.stored)) {
		return { platform: input.stored };
	}
	return { platform: fromUserAgent(input.userAgent) };
}

function readQueryOverride(search: string | null | undefined): string | null {
	if (!search) return null;
	try {
		return new URLSearchParams(search.startsWith('?') ? search.slice(1) : search).get(
			UI_PLATFORM_QUERY_KEY
		);
	} catch {
		return null;
	}
}

function fromUserAgent(userAgent: string | null | undefined): UiPlatform {
	return userAgent && ANDROID_UA_PATTERN.test(userAgent) ? 'mobile' : 'desktop';
}

/**
 * Resolve the platform in a browser: prefer the attribute the inline
 * `app.html` script already stamped, else run detection now and stamp it.
 */
export function resolveUiPlatform(): UiPlatform {
	if (typeof document === 'undefined') return 'desktop';
	const stamped = document.documentElement.getAttribute(UI_PLATFORM_ATTRIBUTE);
	if (isUiPlatform(stamped)) return stamped;

	let stored: string | null = null;
	try {
		stored = localStorage.getItem(UI_PLATFORM_STORAGE_KEY);
	} catch {
		stored = null;
	}
	const result = detectUiPlatform({
		userAgent: typeof navigator === 'undefined' ? null : navigator.userAgent,
		search: typeof location === 'undefined' ? null : location.search,
		stored
	});
	applyPersist(result.persist);
	document.documentElement.setAttribute(UI_PLATFORM_ATTRIBUTE, result.platform);
	return result.platform;
}

function applyPersist(persist: UiPlatform | null | undefined) {
	if (persist === undefined) return;
	try {
		if (persist === null) localStorage.removeItem(UI_PLATFORM_STORAGE_KEY);
		else localStorage.setItem(UI_PLATFORM_STORAGE_KEY, persist);
	} catch {
		// Storage may be unavailable (private mode, quota). The attribute still applies.
	}
}
