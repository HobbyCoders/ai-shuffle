/**
 * Which composer this window shows, and the phone one's sheet.
 *
 * The phone app always has the phone composer. A web window at phone width has
 * the same one, the same code rather than a look-alike: the quick bar, the
 * context line and Chat controls. The web build loads that code only when a
 * window reaches this width, so a desktop window never downloads it.
 */
import type { Component } from 'svelte';
import { platform } from '$lib/stores/platform.svelte';
import { composerShell, type ComposerSheetProps } from '../composer-shell';

/** Width at or below which a web window takes the phone composer. */
export const PHONE_LAYOUT_QUERY = '(max-width: 480px)';

const media = typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(PHONE_LAYOUT_QUERY) : null;

let narrow = $state(media?.matches ?? false);
let loadedSheet = $state<Component<ComposerSheetProps> | null>(null);
let loading = false;

function loadSheet() {
	// The phone app registered its own copy at startup: nothing to fetch.
	if (loading || loadedSheet || composerShell().ControlsSheet) return;
	loading = true;
	void import('./MobileComposerControlsSheet.svelte')
		.then((module) => {
			loadedSheet = module.default as Component<ComposerSheetProps>;
		})
		.finally(() => {
			loading = false;
		});
}

if (media?.matches) loadSheet();
media?.addEventListener?.('change', (event) => {
	narrow = event.matches;
	if (event.matches) loadSheet();
});

export const phoneLayout = {
	/** True when this window shows the phone composer. */
	get active(): boolean {
		return platform.isMobile || narrow;
	},
	/** The controls sheet to render, or null on a wider window (or while it loads). */
	get ControlsSheet(): Component<ComposerSheetProps> | null {
		const registered = composerShell().ControlsSheet;
		if (registered) return registered;
		return narrow ? loadedSheet : null;
	}
};
