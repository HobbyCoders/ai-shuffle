/**
 * Ctrl/Cmd+K: one registration decides what the key does.
 *
 * Spotlight and the Settings page's search used to each register Ctrl+K, and
 * the newest registration won. Which one was newest depended on mount order
 * (opening the app straight onto Settings registered Settings first), so the
 * key's meaning was not predictable. Now the app shell owns the key and asks:
 *
 * 1. Spotlight is open: close it.
 * 2. Settings is on screen: focus the Settings search box (it works from a
 *    field on the Settings page too, as before).
 * 3. Otherwise: open Spotlight (not from inside a text field, as before).
 *
 * Settings says it is on screen through `settingsNav.claimSearchShortcut()`.
 */
import { registerShortcut, type ShortcutRegistration } from './keyboard';
import { settingsNav } from '$lib/settings/navigation.svelte';

export type SearchKeyAction = 'close-spotlight' | 'settings-search' | 'open-spotlight';

export interface SearchKeyState {
	spotlightOpen: boolean;
	settingsOnScreen: boolean;
}

export function searchKeyAction(state: SearchKeyState): SearchKeyAction {
	if (state.spotlightOpen) return 'close-spotlight';
	if (state.settingsOnScreen) return 'settings-search';
	return 'open-spotlight';
}

/** From a text field the key only acts when it will not take the user elsewhere. */
export function searchKeyWorksInField(state: SearchKeyState): boolean {
	return state.spotlightOpen || state.settingsOnScreen;
}

export interface SearchShortcutOptions {
	isSpotlightOpen: () => boolean;
	setSpotlightOpen: (open: boolean) => void;
}

export function registerSearchShortcut(options: SearchShortcutOptions): ShortcutRegistration {
	const state = (): SearchKeyState => ({
		spotlightOpen: options.isSpotlightOpen(),
		settingsOnScreen: settingsNav.searchClaimed
	});
	return registerShortcut({
		id: 'spotlight',
		description: 'Open spotlight search',
		key: 'k',
		cmdOrCtrl: true,
		category: 'navigation',
		allowInInput: () => searchKeyWorksInField(state()),
		action: () => {
			switch (searchKeyAction(state())) {
				case 'close-spotlight':
					options.setSpotlightOpen(false);
					break;
				case 'settings-search':
					settingsNav.requestSearchFocus();
					break;
				case 'open-spotlight':
					options.setSpotlightOpen(true);
					break;
			}
		}
	});
}
