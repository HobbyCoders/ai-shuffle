/**
 * Active Tab Derived Store
 *
 * Provides a cached lookup of the currently active tab.
 * Components subscribe to this instead of scanning the full tabs array.
 *
 * This store re-emits only when the active tab's data actually changes,
 * preventing re-renders when other tabs update.
 */

import { derived } from 'svelte/store';
import { tabs, type ChatTab } from '../tabs';

/**
 * The currently active ChatTab, or undefined if no tab is active.
 * Updates only when the active tab's data changes.
 */
export const activeTab = derived<typeof tabs, ChatTab | undefined>(
	tabs,
	$tabs => $tabs.tabs.find(t => t.id === $tabs.activeTabId)
);

/**
 * The active tab's ID.
 */
export const activeTabId = derived(tabs, $tabs => $tabs.activeTabId);
