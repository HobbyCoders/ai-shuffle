/**
 * Props `AppPage` hands whichever app shell renders it: the desktop
 * `AppLayout` in the worker-served web UI, or the mobile app's own layout
 * (`mobile/src/shell/MobileAppLayout.svelte`). Both shells wrap the same
 * `MainPanel`; only the chrome around it differs.
 */
import type { ViewType } from '$lib/stores/layout.svelte';

export interface AppShellProps {
	isAdmin: boolean;
	onNewSession: () => void;
	onSettledOpenSession: () => void;
	onNewSessionForProject: (projectId: string) => void;
	onOpenSession: (sessionId: string) => void;
	onOpenFeature: (type: ViewType) => void;
	onOpenSpotlight: () => void;
	onOpenPlugins: () => void;
}
