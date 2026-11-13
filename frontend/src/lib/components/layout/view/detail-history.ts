/**
 * A phone detail is one Back step that ViewShell owns (`viewPanelDetail`). A
 * destination may add more steps inside that detail (Docs adds one per document
 * opened from another). Each carries its depth, so ViewShell takes all of them
 * off the stack when the detail closes or another destination opens.
 */
type DetailState = { viewPanelDetail?: string; viewPanelDepth?: number };

/** Extra `page.state` for a step added inside the current phone detail; none outside one. */
export function nestedDetailStep(state: App.PageState): App.PageState {
	const current = state as DetailState;
	return current.viewPanelDetail ? ({ viewPanelDepth: (current.viewPanelDepth ?? 0) + 1 } as App.PageState) : {};
}

/** Leave the phone detail the current entry belongs to, with every step inside it. */
export function leaveDetail(state: App.PageState) {
	const steps = ((state as DetailState).viewPanelDepth ?? 0) + 1;
	if (steps > 1) history.go(-steps);
	else history.back();
}
