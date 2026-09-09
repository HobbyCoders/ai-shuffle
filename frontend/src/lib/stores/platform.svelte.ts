/**
 * Platform store — which UI shell this build renders.
 *
 * `platform.isMobile` is true only in the mobile app's bundle (`mobile/`,
 * built with `VITE_SHUFFLE_SHELL=mobile`, which also calls
 * `setPlatformForMobileApp()` at startup). In the worker-served web UI it is a
 * constant `false`: there is no user-agent sniffing, no `?ui=` override and
 * no localStorage key. Every "is this the mobile UI?" decision in shared
 * components reads this, never `window.innerWidth`. Width-based helpers in
 * `$lib/utils/viewport` exist only for the desktop shell's narrow-window
 * resizing.
 *
 * The value is fixed for the life of the page. `set()` exists for unit tests
 * and for the mobile app's startup only.
 */
import { browser } from '$app/environment';
import {
	buildShell,
	resolveUiPlatform,
	UI_PLATFORM_ATTRIBUTE,
	type UiPlatform
} from '$lib/utils/platform';
import { createSubscribers } from './store-utils';

let _current = $state<UiPlatform>(browser ? resolveUiPlatform() : buildShell());

const { subscribe, notifySubscribers } = createSubscribers<UiPlatform>(
	() => _current,
	(v) => {
		_current = v;
	}
);

export const platform = {
	/** `'mobile'` in the app bundle, `'desktop'` everywhere else. */
	get current(): UiPlatform {
		return _current;
	},
	get isMobile(): boolean {
		return _current === 'mobile';
	},
	get isDesktop(): boolean {
		return _current === 'desktop';
	},
	subscribe,
	/** Test / mobile-app startup hook. Also re-stamps `<html data-platform>` so CSS follows. */
	set(next: UiPlatform) {
		if (_current === next) return;
		_current = next;
		if (typeof document !== 'undefined') {
			document.documentElement.setAttribute(UI_PLATFORM_ATTRIBUTE, next);
		}
		notifySubscribers();
	}
};

/**
 * Called once by the mobile app's root layout before anything renders. The
 * build flag already selects the mobile shell; this makes the choice explicit
 * (and correct under tooling that does not carry Vite env, e.g. unit tests).
 */
export function setPlatformForMobileApp(): void {
	platform.set('mobile');
}

/** Convenience for non-reactive call sites (event handlers, plain .ts modules). */
export function isMobileUi(): boolean {
	return platform.isMobile;
}
