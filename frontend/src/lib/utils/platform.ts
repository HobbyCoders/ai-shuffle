/**
 * UI platform — which shell this *build* renders.
 *
 * The web UI a worker serves is desktop-only: every browser, at any window
 * width, gets the desktop shell and merely resizes. The mobile shell is a
 * separate SvelteKit app (`mobile/`) that shares `$lib` with this one and is
 * bundled into the Android app; it is built with `VITE_SHUFFLE_SHELL=mobile`
 * and stamps the mobile platform attribute on `<html>` in its own `app.html`.
 *
 * So the platform is a build-time decision. It is never derived from the user
 * agent, a query-string override, localStorage, or the viewport width — a
 * worker on any version must not be able to change what the phone shows, and
 * a phone browser must keep the desktop shell. See
 * docs/mobile-desktop-ui-split.md.
 */

export type UiPlatform = 'mobile' | 'desktop';

/** Attribute on `<html>` carrying the platform for CSS (`html[data-platform='…']`). */
export const UI_PLATFORM_ATTRIBUTE = 'data-platform';

/** Vite env flag that selects the shell at build time (`desktop` unless `mobile`). */
export const SHELL_ENV_KEY = 'VITE_SHUFFLE_SHELL';

/**
 * The shell this build was compiled for. Anything other than an explicit
 * `mobile` is the desktop web UI.
 */
export function buildShell(env: Record<string, unknown> | undefined = import.meta.env): UiPlatform {
	return env?.[SHELL_ENV_KEY] === 'mobile' ? 'mobile' : 'desktop';
}

/**
 * Resolve the platform in a browser and make sure `<html data-platform>` says
 * the same thing (the mobile app's `app.html` stamps it before first paint;
 * the web `app.html` stamps `desktop`). The build flag is the source of truth.
 */
export function resolveUiPlatform(): UiPlatform {
	const platform = buildShell();
	if (typeof document !== 'undefined') {
		document.documentElement.setAttribute(UI_PLATFORM_ATTRIBUTE, platform);
	}
	return platform;
}
