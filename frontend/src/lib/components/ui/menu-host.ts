/**
 * menuHost — a Svelte action that moves a dropdown into a host element the
 * caller owns, so a selector's own menu can fill a page somewhere else.
 *
 * The phone's Chat controls (MobileComposerControlsSheet) shows the Agent and
 * "Runs on" choices as full pages that slide in inside the sheet. The menus
 * stay the shared selectors' own (ProfileSelector, ContextPillBar's move menu,
 * ExecutionModeBar's runner menu), with every rule they carry: the action
 * only changes where the node sits. With no host the node stays in place, so
 * the desktop and every other caller are unchanged.
 *
 * Like floatingMenu, a moved node is no longer inside its trigger's root, so
 * that component's "click outside" check must also accept the menu itself.
 * Selectors through the original ancestors stop matching; the host styles the
 * menu by its own classes.
 */
export function menuHost(node: HTMLElement, host: HTMLElement | null | undefined) {
	let moved = false;
	function place(target: HTMLElement | null | undefined) {
		if (target && node.parentNode !== target) {
			target.appendChild(node);
			moved = true;
		}
	}
	place(host);
	return {
		update(next: HTMLElement | null | undefined) {
			place(next);
		},
		destroy() {
			// Svelte removes a closing menu itself, but when an ancestor branch
			// goes (the row it hangs from re-renders) only that ancestor's own
			// nodes are removed: a moved menu would stay behind in the host.
			if (moved) node.remove();
		}
	};
}
