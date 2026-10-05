import { describe, expect, it } from 'vitest';
import { platform } from '$lib/stores/platform.svelte';
import { composerShell, registerComposerShell } from '$lib/components/chat/composer-shell';
import { installMobileShell } from './register';
import { reconcileMobileComposerExpanded } from './mobile-composer-collapse';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import MobilePromptDock from './MobilePromptDock.svelte';

describe('installMobileShell', () => {
	it('selects the mobile platform and registers every mobile composer hook', () => {
		platform.set('desktop');
		expect(composerShell().ControlsSheet).toBeNull();

		installMobileShell();

		expect(platform.isMobile).toBe(true);
		expect(document.documentElement.getAttribute('data-platform')).toBe('mobile');
		const hooks = composerShell();
		expect(hooks.reconcileExpanded).toBe(reconcileMobileComposerExpanded);
		expect(hooks.ControlsSheet).toBe(MobileComposerControlsSheet);
		expect(hooks.PromptDock).toBe(MobilePromptDock);
		// #1816: the composer keeps its quick bar, so it never collapses, and the
		// sheet owns the pull-up gesture; those hooks keep their no-op defaults.
		expect(hooks.shouldCollapse({ compact: true, mobileViewport: true, hasDraft: false, expanded: false,
			composerFocused: false, controlsOpen: false, commandAutocompleteOpen: false, fileAutocompleteOpen: false })).toBe(false);
		expect(hooks.shouldBlockToolbarPointer({ collapsed: false, expandPointerActive: true, mobileViewport: true })).toBe(false);
		expect(hooks.attachComposerOpen(document.createElement('form'), {} as never)).toBeUndefined();
	});

	it('runs once: a re-mounted layout does not register or switch the platform again', () => {
		registerComposerShell({});
		platform.set('desktop');
		installMobileShell();
		expect(composerShell().ControlsSheet).toBeNull();
		expect(platform.isDesktop).toBe(true);
		platform.set('mobile');
	});
});
