import { describe, expect, it } from 'vitest';
import { platform } from '$lib/stores/platform.svelte';
import { composerShell, registerComposerShell } from '$lib/components/chat/composer-shell';
import { installMobileShell } from './register';
import {
	reconcileMobileComposerExpanded,
	shouldBlockMobileComposerToolbarPointer,
	shouldCollapseMobileComposer
} from './mobile-composer-collapse';
import { attachComposerOpen } from './mobile-panel-gesture';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';

describe('installMobileShell', () => {
	it('selects the mobile platform and registers every mobile composer hook', () => {
		platform.set('desktop');
		expect(composerShell().ControlsSheet).toBeNull();

		installMobileShell();

		expect(platform.isMobile).toBe(true);
		expect(document.documentElement.getAttribute('data-platform')).toBe('mobile');
		const hooks = composerShell();
		expect(hooks.shouldCollapse).toBe(shouldCollapseMobileComposer);
		expect(hooks.reconcileExpanded).toBe(reconcileMobileComposerExpanded);
		expect(hooks.shouldBlockToolbarPointer).toBe(shouldBlockMobileComposerToolbarPointer);
		expect(hooks.attachComposerOpen).toBe(attachComposerOpen);
		expect(hooks.ControlsSheet).toBe(MobileComposerControlsSheet);
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
