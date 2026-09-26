/**
 * Mobile shell registration — runs once, before the first render.
 *
 * The shared components in `$lib` carry `platform.isMobile` behaviour
 * switches but none of the mobile-only code. This wires that code in:
 * the collapsed composer, the swipe-to-open gesture and the controls sheet.
 */
import { setPlatformForMobileApp } from '$lib/stores/platform.svelte';
import { registerComposerShell } from '$lib/components/chat/composer-shell';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import {
	reconcileMobileComposerExpanded,
	shouldBlockMobileComposerToolbarPointer,
	shouldCollapseMobileComposer
} from './mobile-composer-collapse';
import { attachComposerOpen } from './mobile-panel-gesture';

let installed = false;

export function installMobileShell(): void {
	if (installed) return;
	installed = true;
	setPlatformForMobileApp();
	registerComposerShell({
		shouldCollapse: shouldCollapseMobileComposer,
		reconcileExpanded: reconcileMobileComposerExpanded,
		shouldBlockToolbarPointer: shouldBlockMobileComposerToolbarPointer,
		attachComposerOpen,
		ControlsSheet: MobileComposerControlsSheet
	});
}
