/**
 * What the Plugins collection last loaded, for search and the index badge.
 * The collection publishes here after every load; before the first visit the
 * index badge asks for the installed list once (drafts needing review).
 */
export interface PluginSearchRow {
	/** Collection item: `plugin:<id>` or `connection:<type>`. */
	item: string;
	title: string;
	where: string;
	keywords?: string;
}

export const pluginsLive = $state({
	loaded: false,
	/** Agent-authored drafts waiting for approval. */
	pending: 0,
	items: [] as PluginSearchRow[]
});

export function publishPlugins(items: PluginSearchRow[], pending: number): void {
	pluginsLive.items = items;
	pluginsLive.pending = pending;
	pluginsLive.loaded = true;
}

let primed = false;

/** One quiet fetch of the installed list so the badge shows before a visit. */
export function primePending(): void {
	if (primed || pluginsLive.loaded || typeof window === 'undefined') return;
	primed = true;
	void import('$lib/api/plugins')
		.then((api) => api.getInstalledPlugins())
		.then((list) => {
			if (pluginsLive.loaded || !Array.isArray(list)) return;
			pluginsLive.pending = list.filter((p) => p?.approval === 'pending').length;
		})
		.catch(() => {});
}

/** Tests only. */
export function resetPluginsLive(): void {
	primed = false;
	pluginsLive.loaded = false;
	pluginsLive.pending = 0;
	pluginsLive.items = [];
}
