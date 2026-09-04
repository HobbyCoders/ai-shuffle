export interface MobileComposerCollapseState {
	compact: boolean;
	hasDraft: boolean;
	expanded: boolean;
	composerFocused: boolean;
	controlsOpen: boolean;
	commandAutocompleteOpen: boolean;
	fileAutocompleteOpen: boolean;
}

export function shouldCollapseMobileComposer(state: MobileComposerCollapseState): boolean {
	return (
		state.compact &&
		!state.hasDraft &&
		!state.expanded &&
		!state.composerFocused &&
		!state.controlsOpen &&
		!state.commandAutocompleteOpen &&
		!state.fileAutocompleteOpen
	);
}

export interface MobileComposerReconcileState {
	compact: boolean;
	hasDraft: boolean;
	composerFocused: boolean;
	controlsOpen: boolean;
	commandAutocompleteOpen: boolean;
	fileAutocompleteOpen: boolean;
	/** Focus currently belongs to the island or a registered composer portal. */
	activeElementOwned: boolean;
}

/**
 * Resolve the expansion latch only after focus/pointer ownership has settled.
 * Successful send is the sole immediate-collapse transition; every other path
 * stays expanded while the user has content or is interacting with composer UI.
 */
export function reconcileMobileComposerExpanded(
	currentExpanded: boolean,
	state: MobileComposerReconcileState,
	reason: 'deferred-focus' | 'successful-send' = 'deferred-focus'
): boolean {
	if (reason === 'successful-send') return false;
	if (!state.compact) return currentExpanded;
	if (
		state.hasDraft ||
		state.composerFocused ||
		state.controlsOpen ||
		state.commandAutocompleteOpen ||
		state.fileAutocompleteOpen ||
		state.activeElementOwned
	) {
		return true;
	}
	return false;
}

export interface MobileComposerToolbarPointerState {
	collapsed: boolean;
	/** True from the expanding pointerdown until after the following click. */
	expandPointerActive: boolean;
}

/**
 * The collapsed dock is `display:none`, but expanding on pointerdown reflows
 * the toolbar under the still-down finger. Block hits while collapsed and
 * until that pointer's click has been delivered.
 */
export function shouldBlockMobileComposerToolbarPointer(
	state: MobileComposerToolbarPointerState
): boolean {
	return state.collapsed || state.expandPointerActive;
}
