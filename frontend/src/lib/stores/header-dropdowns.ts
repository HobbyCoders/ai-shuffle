/**
 * header-dropdowns - Coordinates the single-open-at-a-time behavior of the
 * PageHeader's popover-style controls (profile selector, branch selector,
 * notifications, and git actions).
 *
 * Each control reads `openHeaderDropdown` to decide whether it is open, and
 * sets it to its own id when toggled open. Because exclusivity is driven by
 * this shared store rather than DOM event propagation, opening one control
 * reliably closes the others (e.g. opening the profile selector closes the
 * branch selector, and vice versa).
 */
import { writable } from 'svelte/store';

type HeaderDropdownId =
	| 'profile'
	| 'branch'
	| 'workspace-folders'
	| 'sheet-workspace-folders'
	// Mobile-sheet selectors have distinct ids because their CSS-hidden composer
	// counterparts remain mounted and subscribe to this same coordinator.
	| 'sheet-profile'
	| 'sheet-branch'
	| 'runner'
	| 'notifications'
	// Bound-chat session title in the header breadcrumb (rename / fork / delete).
	| 'session-title'
	// Git-action option popovers (split-button chevrons). Listing them here means
	// opening any one closes every other header dropdown — including the branch
	// selector and any sibling git-action popover — so only one is ever open.
	| 'git-commit'
	| 'git-push'
	| 'git-pull'
	| 'git-pr'
	| 'git-finish'
	| 'git-shipit';

/** The currently-open header dropdown, or null when all are closed. */
export const openHeaderDropdown = writable<HeaderDropdownId | null>(null);

/** Open the given dropdown, implicitly closing any other. */
export function openDropdown(id: HeaderDropdownId) {
	openHeaderDropdown.set(id);
}

/** Close the given dropdown only if it is the one currently open. */
export function closeDropdown(id: HeaderDropdownId) {
	openHeaderDropdown.update((cur) => (cur === id ? null : cur));
}

/** Toggle the given dropdown; returns true if it is now open. */
export function toggleDropdown(id: HeaderDropdownId): boolean {
	let nowOpen = false;
	openHeaderDropdown.update((cur) => {
		nowOpen = cur !== id;
		return nowOpen ? id : null;
	});
	return nowOpen;
}
