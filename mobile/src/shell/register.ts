/**
 * Mobile shell registration — runs once, before the first render.
 *
 * The shared components in `$lib` carry `platform.isMobile` behaviour
 * switches but none of the mobile-only code. This wires that code in: the
 * composer's quick bar and chat-controls panel, and the prompt dock above
 * the composer (issue #1816).
 *
 * The composer no longer collapses to one line on the phone: the quick bar
 * under the text field is always there, and pulling it up opens the panel
 * (the sheet owns that gesture), so the old collapse and swipe hooks stay
 * unregistered (their desktop defaults are no-ops).
 */
import { setPlatformForMobileApp } from '$lib/stores/platform.svelte';
import { registerComposerShell } from '$lib/components/chat/composer-shell';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import MobilePromptDock from './MobilePromptDock.svelte';
import { reconcileMobileComposerExpanded } from './mobile-composer-collapse';

let installed = false;

export function installMobileShell(): void {
	if (installed) return;
	installed = true;
	setPlatformForMobileApp();
	registerComposerShell({
		reconcileExpanded: reconcileMobileComposerExpanded,
		ControlsSheet: MobileComposerControlsSheet,
		PromptDock: MobilePromptDock
	});
}
