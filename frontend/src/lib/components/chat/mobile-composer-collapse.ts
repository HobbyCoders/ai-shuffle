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
