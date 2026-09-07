/**
 * Platform store — which UI shell this session renders.
 *
 * `platform.isMobile` is true only inside the Android app (or under a
 * `?ui=mobile` dev override). Every "is this the mobile UI?" decision in
 * components reads this, never `window.innerWidth`. Width-based helpers in
 * `$lib/utils/viewport` exist only for the desktop shell's narrow-window
 * resizing.
 *
 * The value is fixed for the life of the page: it is resolved once from the
 * `<html data-platform>` attribute stamped before first paint. `set()` exists
 * for tests and the dev override only.
 */
import { browser } from '$app/environment';
import {
	resolveUiPlatform,
	UI_PLATFORM_ATTRIBUTE,
	type UiPlatform
} from '$lib/utils/platform';
import { createSubscribers } from './store-utils';

let _current = $state<UiPlatform>(browser ? resolveUiPlatform() : 'desktop');

const { subscribe, notifySubscribers } = createSubscribers<UiPlatform>(
	() => _current,
	(v) => {
		_current = v;
	}
);

export const platform = {
	/** `'mobile'` inside the Android app, `'desktop'` everywhere else. */
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
	/** Test / dev-override hook. Also re-stamps `<html data-platform>` so CSS follows. */
	set(next: UiPlatform) {
		if (_current === next) return;
		_current = next;
		if (typeof document !== 'undefined') {
			document.documentElement.setAttribute(UI_PLATFORM_ATTRIBUTE, next);
		}
		notifySubscribers();
	}
};

/** Convenience for non-reactive call sites (event handlers, plain .ts modules). */
export function isMobileUi(): boolean {
	return platform.isMobile;
}
