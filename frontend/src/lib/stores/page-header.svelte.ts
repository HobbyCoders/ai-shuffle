/**
 * Page header slot.
 *
 * In the desktop app the top strip is the only header: feature pages hand
 * their action buttons (New note, New automation, the docs search…) to it
 * instead of drawing their own page header underneath. `StudioPageHeader`
 * claims the slot while its page is the active feature panel and releases it
 * when the page is hidden, so the strip always shows the actions of the page
 * on screen and nothing when a chat is open.
 *
 * Claims are tokened: a page that is torn down while another has already
 * claimed the slot cannot clear the newer owner's actions.
 */
import type { Snippet } from 'svelte';

let _actions = $state.raw<Snippet | null>(null);
let _owner: symbol | null = null;

export const pageHeader = {
	/** Actions of the page currently on screen, or null. */
	get actions(): Snippet | null {
		return _actions;
	},
	claim(token: symbol, actions: Snippet | null): void {
		_owner = token;
		_actions = actions;
	},
	release(token: symbol): void {
		if (_owner !== token) return;
		_owner = null;
		_actions = null;
	}
};
