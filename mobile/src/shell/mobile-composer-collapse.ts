import type {
	MobileComposerCollapseState,
	MobileComposerReconcileReason,
	MobileComposerReconcileState,
	MobileComposerToolbarPointerState
} from '$lib/components/chat/composer-shell';

// The state shapes live on the seam ($lib composer-shell) that these
// implementations are registered into; re-exported for the tests here.
export type { MobileComposerCollapseState, MobileComposerReconcileState };

export function shouldCollapseMobileComposer(state: MobileComposerCollapseState): boolean {
	return (
		state.mobileViewport &&
		state.compact &&
		!state.hasDraft &&
		!state.expanded &&
		!state.composerFocused &&
		!state.controlsOpen &&
		!state.commandAutocompleteOpen &&
		!state.fileAutocompleteOpen
	);
}

/**
 * Resolve the expansion latch only after focus/pointer ownership has settled.
 * Successful send is the sole immediate-collapse transition; every other path
 * stays expanded while the user has content or is interacting with composer UI.
 */
export function reconcileMobileComposerExpanded(
	currentExpanded: boolean,
	state: MobileComposerReconcileState,
	reason: MobileComposerReconcileReason = 'deferred-focus'
): boolean {
	if (reason === 'successful-send') return false;
	if (!state.compact) return currentExpanded;
	if (
		state.hasDraft ||
		state.composerFocused ||
		state.controlsOpen ||
		state.commandAutocompleteOpen ||
		state.fileAutocompleteOpen ||
		state.activeElementOwned ||
		state.interactionOwned
	) {
		return true;
	}
	return false;
}

/**
 * The collapsed dock is `display:none`, but expanding on pointerdown reflows
 * the toolbar under the still-down finger. Block hits while collapsed and
 * until that pointer's click has been delivered. Never block outside the
 * mobile shell — `compact` is true on desktop for any active chat.
 */
export function shouldBlockMobileComposerToolbarPointer(
	state: MobileComposerToolbarPointerState
): boolean {
	if (!state.mobileViewport) return false;
	return state.collapsed || state.expandPointerActive;
}
