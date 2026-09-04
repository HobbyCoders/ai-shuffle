/**
 * The shape the sidebar hands to its Workspace nav and Customize dialog.
 *
 * Sidebar.svelte builds the list (it owns the admin gating and knows which
 * destination the main panel is currently showing); WorkspaceNav renders it and
 * WorkspaceCustomize edits its order and visibility. `id` doubles as the
 * `ViewType` the row opens, which is why neither of those components needs a
 * lookup table to call `onOpenFeature` or `preloadPanelForView`.
 */
import { Files } from 'lucide-svelte';
import type { WorkspaceItemId } from '$lib/stores/sidebar-prefs.svelte';

/** Any lucide-svelte icon component; `Files` is only a stand-in for the shape. */
export type WorkspaceIcon = typeof Files;

export interface WorkspaceNavItem {
	id: WorkspaceItemId;
	label: string;
	icon: WorkspaceIcon;
	/** True when this destination is the one the main panel is showing. */
	on: boolean;
}
