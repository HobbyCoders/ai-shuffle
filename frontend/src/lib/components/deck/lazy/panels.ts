/**
 * The lazy-chunk registry for every secondary surface in the main panel.
 *
 * The chat surface is deliberately absent: ChatView, MessageArea, the composer
 * and the git header cluster stay statically imported, because a spinner on the
 * core interaction would be a regression, not an optimisation. Everything here
 * is something the user navigates *to*.
 *
 * `preloadPanelForView()` is the intent hook: call it on hover/focus of a nav
 * item and the chunk is usually resolved before the click lands, so LazyPanel
 * renders it synchronously and the navigation feels instant.
 */
import { createLazyComponent, type LazyComponentHandle } from './lazy-component';

export const SettingsPanel = createLazyComponent('settings', () =>
	import('$lib/components/deck/cards/SettingsCard.svelte')
);
export const ProjectPanel = createLazyComponent('project', () =>
	import('$lib/components/deck/cards/ProjectCard.svelte')
);
export const FileBrowserPanel = createLazyComponent('file-browser', () =>
	import('$lib/components/deck/cards/FileBrowserCard.svelte')
);
export const NotesPanel = createLazyComponent('notes', () =>
	import('$lib/components/deck/cards/NotesCard.svelte')
);
export const AutomationsPanel = createLazyComponent('automations', () =>
	import('$lib/components/deck/cards/AutomationsCard.svelte')
);
export const StatsPanel = createLazyComponent('stats', () =>
	import('$lib/components/deck/cards/StatsCard.svelte')
);
export const PrInboxPanel = createLazyComponent('prs', () =>
	import('$lib/components/deck/cards/PrInboxCard.svelte')
);
export const BuildPanel = createLazyComponent('build', () =>
	import('$lib/components/layout/BuildPage.svelte')
);
/**
 * Docs additionally waits on the markdown chunk. The registry, the nav and the
 * search index all read `DocEntry.content` synchronously, so the content must
 * be resident before DocsCard mounts — `prepare` keeps that contract without
 * pushing 404 kB of markdown into anyone else's download.
 */
export const DocsPanel = createLazyComponent(
	'docs',
	() => import('$lib/components/docs/DocsCard.svelte'),
	{ prepare: () => import('$lib/docs/index').then((m) => m.preloadAllDocs()) }
);

// ── Surfaces that are not nav destinations ──────────────────────────────────
export const BrowserSidePanel = createLazyComponent('browser-panel', () =>
	import('$lib/components/browser/BrowserPanel.svelte')
);
export const ChangedFilesPanelLazy = createLazyComponent('changed-files', () =>
	import('$lib/components/git/ChangedFilesPanel.svelte')
);
export const CreateBranchModalLazy = createLazyComponent('create-branch-modal', () =>
	import('$lib/components/git/CreateBranchModal.svelte')
);
export const InitRepoModalLazy = createLazyComponent('init-repo-modal', () =>
	import('$lib/components/git/InitRepoModal.svelte')
);

/** Feature-panel keys, i.e. the `{#if}` branches MainPanel used to hard-mount. */
export type FeaturePanelKey =
	| 'build'
	| 'project'
	| 'settings'
	| 'file-browser'
	| 'notes'
	| 'automations'
	| 'prs'
	| 'stats'
	| 'docs';

/** The Build page hosts these view types as sub-tabs. */
const BUILD_MEMBER_VIEWS = new Set(['build', 'profile', 'mcp', 'hooks']);
/**
 * Settings hosts plugin management. `plugins` used to be a Build sub-tab; the
 * view type stays so existing "Manage plugins" deep links land in Settings.
 */
const SETTINGS_MEMBER_VIEWS = new Set(['settings', 'plugins']);

const PANEL_BY_KEY: Record<FeaturePanelKey, LazyComponentHandle<unknown>> = {
	build: BuildPanel,
	project: ProjectPanel,
	settings: SettingsPanel,
	'file-browser': FileBrowserPanel,
	notes: NotesPanel,
	automations: AutomationsPanel,
	prs: PrInboxPanel,
	stats: StatsPanel,
	docs: DocsPanel
};

/** Resolve a layout view type to the feature panel that renders it, or null for chat. */
export function featurePanelKeyForView(viewType: string | undefined | null): FeaturePanelKey | null {
	if (!viewType) return null;
	if (BUILD_MEMBER_VIEWS.has(viewType)) return 'build';
	if (SETTINGS_MEMBER_VIEWS.has(viewType)) return 'settings';
	return viewType in PANEL_BY_KEY ? (viewType as FeaturePanelKey) : null;
}

export function panelForKey(key: FeaturePanelKey): LazyComponentHandle<unknown> {
	return PANEL_BY_KEY[key];
}

/**
 * Intent prefetch. Safe to call on every pointerenter — the handle dedupes and
 * a resolved chunk is a no-op.
 */
export function preloadPanelForView(viewType: string | undefined | null): void {
	const key = featurePanelKeyForView(viewType);
	if (key) PANEL_BY_KEY[key].preload();
}

/**
 * Idle warm-up order. Cheap, high-traffic surfaces first; Docs last because it
 * drags the markdown chunk along.
 */
export const IDLE_PRELOAD_ORDER: readonly LazyComponentHandle<unknown>[] = [
	BuildPanel,
	SettingsPanel,
	ProjectPanel,
	NotesPanel,
	FileBrowserPanel,
	StatsPanel,
	AutomationsPanel,
	PrInboxPanel
];
