/**
 * Composer shell hooks — the seam between the shared composer and the mobile
 * app's collapse/gesture/sheet behaviour.
 *
 * `ChatInput` and `ControlsDock` are shared by the desktop web UI and the
 * mobile app (`mobile/`). The mobile-only parts — the collapsed composer, the
 * swipe-to-open gesture and the controls sheet — live in `mobile/src/shell`
 * and are registered here by the mobile app at startup via
 * `registerComposerShell()`. The web build never registers anything, so the
 * defaults below are pure desktop behaviour: nothing collapses, no gesture is
 * attached, and no sheet is rendered.
 *
 * Keeping the hooks in `$lib` (rather than importing mobile code into shared
 * components) is what lets the web build ship without a single line of the
 * mobile UI.
 */
import type { Component } from 'svelte';
import type { PickerGroup } from '$lib/constants/model-picker';
import type { ContextWindowChoice } from '$lib/constants/model-context';

// ─── Collapse state ─────────────────────────────────────────────────────────

export interface MobileComposerCollapseState {
	compact: boolean;
	/** True on the mobile shell (`platform.isMobile`). `compact` is also true on desktop once a chat has messages. */
	mobileViewport: boolean;
	hasDraft: boolean;
	expanded: boolean;
	composerFocused: boolean;
	controlsOpen: boolean;
	commandAutocompleteOpen: boolean;
	fileAutocompleteOpen: boolean;
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
	/** Last interaction belongs to the composer, even if a control lost focus. */
	interactionOwned?: boolean;
}

export type MobileComposerReconcileReason = 'deferred-focus' | 'successful-send';

export interface MobileComposerToolbarPointerState {
	collapsed: boolean;
	/** True from the expanding pointerdown until after the following click. */
	expandPointerActive: boolean;
	/** Collapse/inert is a mobile-shell concern; desktop must keep the dock clickable. */
	mobileViewport: boolean;
}

// ─── Gesture ────────────────────────────────────────────────────────────────

export interface ComposerOpenOptions {
	enabled: () => boolean;
	allowEditor: () => boolean;
	/** Whether the controls sheet is open right now; the gesture toggles it. */
	open: () => boolean;
	/** Which way a swipe opens the sheet for the visible layout; closing is the reverse. */
	openDirection: () => 'up' | 'down';
	onOpenChange: (open: boolean) => void;
}

export interface ComposerGestureHandle {
	cancel: () => void;
	destroy: () => void;
}

// ─── Controls sheet ─────────────────────────────────────────────────────────

export interface ComposerControlOption {
	value: string;
	label: string;
	description: string;
}

export interface ComposerModelOption extends ComposerControlOption {
	group: string;
	groupKey: string;
	groupSubtext: string | null;
}

/** Props `ControlsDock` hands the mobile controls sheet. */
export interface ComposerSheetProps {
	open: boolean;
	onClose: () => void;
	panelId?: string;
	inertToolbar?: boolean;
	modelOptions: ComposerModelOption[];
	modelGroups?: PickerGroup[];
	modelValue: string;
	modelLabel: string;
	modelLocked?: boolean;
	modelNotice?: string;
	onModelSelect: (value: string) => void;
	contextChoice: ContextWindowChoice;
	onContextChoiceSelect: (choice: ContextWindowChoice) => void;
	effortOptions: ComposerControlOption[];
	effortExplanation?: string;
	effortKnown?: boolean;
	effortValue: string | null;
	onEffortSelect: (value: string) => void;
	fastSupported?: boolean;
	fastEnabled?: boolean;
	onToggleFast?: () => void;
	showCodeControls?: boolean;
	/** Agent, workspace, branch and runner selectors (off in the assistant's home). */
	showSetup?: boolean;
	/** Effort and fast mode (off in the assistant's home). */
	showEffort?: boolean;
	permissionOptions: ComposerControlOption[];
	permissionValue: string | null;
	/** The session write is pending; keep the saved choice visible until it succeeds. */
	permissionUpdating?: boolean;
	onPermissionSelect: (value: string) => void;
}

// ─── Registry ───────────────────────────────────────────────────────────────

export interface ComposerShellHooks {
	/** Whether the composer should render collapsed (mobile shell only). */
	shouldCollapse: (state: MobileComposerCollapseState) => boolean;
	/** Resolve the expansion latch after focus/pointer ownership settles. */
	reconcileExpanded: (
		currentExpanded: boolean,
		state: MobileComposerReconcileState,
		reason?: MobileComposerReconcileReason
	) => boolean;
	/** Whether toolbar hits are blocked while the collapsed dock reflows. */
	shouldBlockToolbarPointer: (state: MobileComposerToolbarPointerState) => boolean;
	/** Attach the swipe-up-to-open gesture; returns a handle or nothing (desktop). */
	attachComposerOpen: (composer: HTMLElement, options: ComposerOpenOptions) => ComposerGestureHandle | undefined;
	/** The controls sheet component, or `null` when this build has none. */
	ControlsSheet: Component<ComposerSheetProps> | null;
}

const DESKTOP_HOOKS: ComposerShellHooks = {
	shouldCollapse: () => false,
	reconcileExpanded: (currentExpanded) => currentExpanded,
	shouldBlockToolbarPointer: () => false,
	attachComposerOpen: () => undefined,
	ControlsSheet: null
};

let hooks: ComposerShellHooks = DESKTOP_HOOKS;

/** The hooks in effect for this build. */
export function composerShell(): ComposerShellHooks {
	return hooks;
}

/** Mobile app startup: install the mobile composer behaviour. */
export function registerComposerShell(next: Partial<ComposerShellHooks>): void {
	hooks = { ...DESKTOP_HOOKS, ...next };
}
