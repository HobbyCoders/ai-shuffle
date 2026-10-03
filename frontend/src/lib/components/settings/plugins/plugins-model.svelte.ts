/**
 * Plugins state for Settings: packages (installed, available, agent-authored
 * drafts), connector runtimes with their accounts, plugin sources, updates
 * and auto-update. Load, install, enable, approve, update and remove live
 * here so the collection and its details stay thin and this stays testable.
 *
 * Connector types, accounts, sources and updates are owner-only (main's
 * plugin hub loaded them only for admins); a read-only device sees the
 * packages.
 */
import { getContext, setContext } from 'svelte';
import { api } from '$lib/api/client';
import { getConnectors, getConnectorTypes, type Connector, type ConnectorTypeInfo } from '$lib/api/connectors';
import * as pluginsApi from '$lib/api/plugins';
import type {
	AutoUpdateSettings,
	AvailablePlugin,
	MarketplaceInfo,
	PluginDetails,
	PluginInfo,
	PluginUpdateInfo,
	ProjectPlugin
} from '$lib/api/plugins';
import type { Project } from '$lib/stores/types';
import { NEEDS_REVIEW } from './plugin-parts';
import { isForbidden } from '$lib/settings/access.svelte';
import { CONNECTION_TYPES } from '$lib/settings/entries/plugins';
import { publishPlugins, type PluginSearchRow } from '$lib/settings/entries/plugins-live.svelte';

export interface RuntimeRef {
	id: string;
	name: string;
	active: boolean;
	automations: { id: string; name: string; is_enabled: boolean }[];
}

/** `busy` while a batch install runs. */
export const BATCH = 'batch-install';

export type RuntimeAction = 'install' | 'enable' | 'disable' | 'uninstall' | 'rollback';

export interface PluginEntry {
	/** Collection item id: `plugin:<id>` or `connection:<type>`. */
	item: string;
	/** Plugin id (packages) or connector type (connections). */
	id: string;
	kind: 'connection' | 'package';
	name: string;
	description: string;
	installed: boolean;
	enabled: boolean;
	/** An agent wrote or installed it and it waits for the owner (approval "pending"). */
	review: boolean;
	authored: boolean;
	version?: string;
	marketplace?: string;
	/** Connector type (connections only). */
	type?: ConnectorTypeInfo;
	/** Accounts of this connector type. */
	count: number;
	/** An account of this type reports an error. */
	failing: boolean;
	update?: PluginUpdateInfo;
	info?: PluginInfo | AvailablePlugin;
}

export const pluginItem = (id: string) => `plugin:${id}`;
export const connectionItem = (type: string) => `connection:${type}`;

export type ParsedItem =
	| { kind: 'plugin'; id: string }
	| { kind: 'connection'; type: string }
	| { kind: 'explore' }
	| { kind: 'sources' }
	| { kind: 'projects' };

export function parseItem(item: string | null | undefined): ParsedItem | null {
	if (!item) return null;
	if (item === 'explore' || item === 'sources' || item === 'projects') return { kind: item };
	if (item.startsWith('plugin:')) return { kind: 'plugin', id: item.slice(7) };
	if (item.startsWith('connection:')) return { kind: 'connection', type: item.slice(11) };
	return null;
}

export function errorText(err: unknown, fallback: string): string {
	if (typeof err === 'object' && err !== null) {
		const e = err as { detail?: unknown; message?: unknown };
		if (typeof e.detail === 'string' && e.detail) return e.detail;
		if (typeof e.message === 'string' && e.message) return e.message;
	}
	return fallback;
}

/** Capability words for a package ("skills, commands"). */
export function capabilities(
	p: Pick<PluginInfo, 'has_skills' | 'has_commands' | 'has_agents' | 'has_hooks' | 'has_mcp'>
): string[] {
	const out: string[] = [];
	if (p.has_skills) out.push('skills');
	if (p.has_commands) out.push('commands');
	if (p.has_agents) out.push('agents');
	if (p.has_hooks) out.push('hooks');
	if (p.has_mcp) out.push('MCP');
	return out;
}

export function plural(n: number, word: string): string {
	return `${n} ${word}${n === 1 ? '' : 's'}`;
}

/** A catalog entry a source lists but this app cannot install. */
export interface SkippedRow {
	key: string;
	name: string;
	reason: string;
	marketplace: string;
}

/** Plugins found in one project's folders. */
export interface ProjectPlugins {
	project: Pick<Project, 'id' | 'name'>;
	plugins: ProjectPlugin[];
	error?: string;
}

export class PluginsModel {
	readonly canManage: () => boolean;

	loading = $state(true);
	/** Load failure (not a 403). */
	error = $state('');
	forbidden = $state(false);
	/** Last failed action. */
	actionError = $state('');

	installed = $state<PluginInfo[]>([]);
	available = $state<AvailablePlugin[]>([]);
	types = $state<ConnectorTypeInfo[]>([]);
	connections = $state<Connector[]>([]);
	marketplaces = $state<MarketplaceInfo[]>([]);
	updates = $state<PluginUpdateInfo[]>([]);
	autoUpdate = $state<AutoUpdateSettings | null>(null);

	details = $state<Record<string, PluginDetails>>({});
	detailsError = $state<Record<string, string>>({});
	refs = $state<Record<string, RuntimeRef[]>>({});

	/** Plugin id or connector type being changed. */
	busy = $state('');
	approving = $state(false);
	checking = $state(false);
	updatingAll = $state(false);
	updating = $state<Record<string, boolean>>({});
	syncing = $state<Record<string, boolean>>({});
	addingSource = $state(false);

	/** Project-scope plugins, by project; loaded when that pane opens. */
	projectPlugins = $state<ProjectPlugins[]>([]);
	projectsLoading = $state(false);
	projectsLoaded = $state(false);
	projectsError = $state('');

	#generation = 0;

	constructor(options: { canManage?: () => boolean } = {}) {
		this.canManage = options.canManage ?? (() => true);
	}

	readonly connectionEntries: PluginEntry[] = $derived(
		this.types.map((t) => {
			const accounts = this.connections.filter((c) => c.type === t.type);
			return {
				item: connectionItem(t.type),
				id: t.type,
				kind: 'connection' as const,
				name: t.name,
				description: CONNECTION_TYPES[t.type]?.description || t.description,
				installed: !!t.plugin_installed,
				enabled: !!t.plugin_enabled,
				review: false,
				authored: false,
				version: t.plugin_version,
				type: t,
				count: accounts.length,
				failing: accounts.some((c) => c.status === 'error')
			};
		})
	);

	readonly packageEntries: PluginEntry[] = $derived.by(() => {
		const installedIds = new Set(this.installed.map((p) => p.id));
		const updates = new Map(this.updates.map((u) => [u.plugin_id, u]));
		const all: (PluginInfo | AvailablePlugin)[] = [
			...this.installed,
			...this.available.filter((p) => !installedIds.has(p.id))
		];
		return all
			.filter((p) => !p.connector_type)
			.map((p) => {
				const record = installedIds.has(p.id) ? (p as PluginInfo) : undefined;
				return {
					item: pluginItem(p.id),
					id: p.id,
					kind: 'package' as const,
					name: p.name,
					description: p.description,
					installed: !!record,
					enabled: record?.enabled ?? false,
					review: record?.approval === 'pending',
					authored: record?.trust === 'authored' || p.id.endsWith('@authored'),
					version: record?.version,
					marketplace: p.marketplace,
					count: 0,
					failing: false,
					update: updates.get(p.id),
					info: p
				};
			});
	});

	readonly entries: PluginEntry[] = $derived([...this.connectionEntries, ...this.packageEntries]);
	/** Drafts waiting for review. */
	readonly review = $derived(this.packageEntries.filter((e) => e.review));
	/** Apps with a runtime installed or accounts set up. */
	readonly apps = $derived(this.connectionEntries.filter((e) => e.installed || e.count > 0));
	readonly installedPackages = $derived(this.packageEntries.filter((e) => e.installed && !e.review));
	readonly availableCount = $derived(this.entries.filter((e) => !e.installed && e.count === 0).length);
	/** Catalog entries the sources list but this app cannot install, with why. */
	readonly skipped: SkippedRow[] = $derived(
		this.marketplaces.flatMap((m) =>
			(m.skipped ?? []).map((s, i) => ({
				key: `${m.id}:${i}`,
				name: s.name,
				reason: s.reason,
				marketplace: m.id
			}))
		)
	);

	entryByItem(item: string | null | undefined): PluginEntry | undefined {
		return item ? this.entries.find((e) => e.item === item) : undefined;
	}

	/**
	 * Map a deep-link item onto this collection's ids: bare plugin ids, bare
	 * connector types and connector account ids (main's hub accepted those).
	 */
	resolveItem(raw: string): string | null {
		if (parseItem(raw)) return raw;
		if (this.packageEntries.some((e) => e.id === raw)) return pluginItem(raw);
		if (this.types.some((t) => t.type === raw)) return connectionItem(raw);
		const account = this.connections.find((c) => c.id === raw);
		return account ? connectionItem(account.type) : null;
	}

	async load(): Promise<void> {
		const request = ++this.#generation;
		const admin = this.canManage();
		try {
			const [installed, available, types, connections] = await Promise.all([
				pluginsApi.getInstalledPlugins(),
				pluginsApi.getAvailablePlugins(),
				admin ? getConnectorTypes() : Promise.resolve([] as ConnectorTypeInfo[]),
				admin ? getConnectors() : Promise.resolve([] as Connector[])
			]);
			if (request !== this.#generation) return;
			this.installed = installed;
			this.available = available;
			this.types = types;
			this.connections = connections;
			this.error = '';
			this.forbidden = false;
			this.#publish();
			this.loading = false;
			if (admin) await this.loadManagement();
		} catch (err) {
			if (request !== this.#generation) return;
			if (isForbidden(err)) this.forbidden = true;
			else this.error = errorText(err, 'Could not load plugins.');
		} finally {
			if (request === this.#generation) this.loading = false;
		}
	}

	/** Sources, updates and auto-update (owner only). Failures leave them empty. */
	async loadManagement(): Promise<void> {
		const [marketplaces, updates, autoUpdate] = await Promise.all([
			pluginsApi.getMarketplaces().catch(() => this.marketplaces),
			pluginsApi.checkForUpdates().catch(() => this.updates),
			pluginsApi.getAutoUpdateSettings().catch(() => this.autoUpdate)
		]);
		this.marketplaces = marketplaces ?? [];
		this.updates = updates ?? [];
		this.autoUpdate = autoUpdate ?? null;
	}

	dispose(): void {
		this.#generation++;
	}

	#publish(): void {
		const rows: PluginSearchRow[] = this.entries.map((e) => ({
			item: e.item,
			title: e.name,
			where:
				e.kind === 'connection'
					? 'Plugins, Apps'
					: e.review
						? `Plugins, ${NEEDS_REVIEW}`
						: e.installed
							? 'Plugins, Installed'
							: 'Plugins, Catalog',
			keywords: `${e.description ?? ''} ${e.kind === 'connection' ? 'connector account' : 'plugin package'}`
		}));
		publishPlugins(rows, this.review.length);
	}

	async #run(id: string, action: () => Promise<void>, fallback: string): Promise<boolean> {
		this.busy = id;
		this.actionError = '';
		try {
			await action();
			return true;
		} catch (err) {
			this.actionError = errorText(err, fallback);
			return false;
		} finally {
			if (this.busy === id) this.busy = '';
		}
	}

	async loadDetails(id: string): Promise<void> {
		try {
			const result = await pluginsApi.getPluginDetails(id);
			this.details = { ...this.details, [id]: result };
			const { [id]: _gone, ...rest } = this.detailsError;
			this.detailsError = rest;
		} catch (err) {
			this.detailsError = {
				...this.detailsError,
				[id]: errorText(err, 'Could not load plugin details.')
			};
		}
	}

	/** Install switched off: the owner sees what it runs before turning it on. */
	install(id: string): Promise<boolean> {
		return this.#run(
			id,
			async () => {
				await pluginsApi.installPlugin(id, 'user', false);
				await this.load();
				await this.loadDetails(id);
			},
			'Could not install plugin.'
		);
	}

	/** Install several packages in one request (Explore's batch install). */
	installMany(ids: string[]): Promise<boolean> {
		if (!ids.length) return Promise.resolve(true);
		return this.#run(
			BATCH,
			async () => {
				await pluginsApi.installPluginsBatch(ids, 'user', false);
				await this.load();
			},
			'Could not install plugins.'
		);
	}

	/** Enable or disable an installed package. Resolves false on failure (the switch flips back). */
	setEnabled(id: string, on: boolean): Promise<boolean> {
		return this.#run(
			id,
			async () => {
				if (on) await pluginsApi.enablePlugin(id);
				else await pluginsApi.disablePlugin(id);
				this.installed = this.installed.map((p) => (p.id === id ? { ...p, enabled: on } : p));
				const detail = this.details[id];
				if (detail) this.details = { ...this.details, [id]: { ...detail, enabled: on } };
				this.#publish();
			},
			on ? 'Could not enable plugin.' : 'Could not disable plugin.'
		);
	}

	/**
	 * Approve a plugin waiting for the owner. An agent-authored draft is
	 * enabled pinned to the content hash of the version that was reviewed, so
	 * a later change needs a new review; an agent install is simply enabled.
	 */
	async approve(id: string): Promise<boolean> {
		const plugin = this.details[id];
		if (!plugin) return false;
		const authored = plugin.trust === 'authored' || id.endsWith('@authored');
		if (authored && !plugin.content_hash) return false;
		this.approving = true;
		this.actionError = '';
		try {
			await pluginsApi.enablePlugin(id, plugin.content_hash);
			await this.load();
			await this.loadDetails(id);
			return true;
		} catch (err) {
			this.actionError = errorText(err, 'Could not enable plugin.');
			return false;
		} finally {
			this.approving = false;
		}
	}

	uninstall(id: string): Promise<boolean> {
		return this.#run(
			id,
			async () => {
				await pluginsApi.uninstallPlugin(id);
				const { [id]: _gone, ...rest } = this.details;
				this.details = rest;
				await this.load();
			},
			'Could not uninstall plugin.'
		);
	}

	async update(id: string): Promise<boolean> {
		this.updating = { ...this.updating, [id]: true };
		try {
			return await this.#run(
				id,
				async () => {
					await pluginsApi.updatePlugin(id);
					await this.load();
					if (this.details[id]) await this.loadDetails(id);
				},
				'Could not update plugin.'
			);
		} finally {
			this.updating = { ...this.updating, [id]: false };
		}
	}

	async checkUpdates(): Promise<void> {
		this.checking = true;
		this.actionError = '';
		try {
			this.updates = await pluginsApi.checkForUpdates();
		} catch (err) {
			this.actionError = errorText(err, 'Could not check for updates.');
		} finally {
			this.checking = false;
		}
	}

	async updateAll(): Promise<void> {
		this.updatingAll = true;
		this.actionError = '';
		try {
			await pluginsApi.updateAllPlugins();
			await this.load();
		} catch (err) {
			this.actionError = errorText(err, 'Could not update plugins.');
		} finally {
			this.updatingAll = false;
		}
	}

	/** Install, enable, disable, uninstall or roll back a connector runtime. */
	changeRuntime(type: string, action: RuntimeAction): Promise<boolean> {
		const pluginId = this.types.find((t) => t.type === type)?.plugin_id;
		if (!pluginId) return Promise.resolve(false);
		return this.#run(
			type,
			async () => {
				if (action === 'rollback') {
					await api.post(`/plugins/runtime/${encodeURIComponent(pluginId)}/rollback`);
				} else {
					const operation = {
						install: pluginsApi.installPlugin,
						enable: pluginsApi.enablePlugin,
						disable: pluginsApi.disablePlugin,
						uninstall: pluginsApi.uninstallPlugin
					}[action];
					await operation(pluginId);
				}
				await this.load();
				await this.loadReferences(type);
			},
			'Could not change the connector plugin.'
		);
	}

	/** Connections (and their automations) that use a connector runtime. */
	async loadReferences(type: string): Promise<void> {
		const pluginId = this.types.find((t) => t.type === type)?.plugin_id;
		if (!pluginId) return;
		try {
			const refs = await api.get<RuntimeRef[]>(`/plugins/runtime/${encodeURIComponent(pluginId)}/connections`);
			this.refs = { ...this.refs, [type]: Array.isArray(refs) ? refs : [] };
		} catch (err) {
			this.actionError = errorText(err, 'Could not load plugin dependencies.');
			this.refs = { ...this.refs, [type]: [] };
		}
	}

	/** Add a git source. An empty name lets the backend derive one from the URL. */
	async addSource(url: string, name = ''): Promise<boolean> {
		if (!url.trim()) return false;
		this.addingSource = true;
		this.actionError = '';
		try {
			const source = await pluginsApi.addMarketplace(url.trim(), name.trim() || undefined);
			this.marketplaces = [...this.marketplaces, source];
			await this.load();
			return true;
		} catch (err) {
			this.actionError = errorText(err, 'Could not add source.');
			return false;
		} finally {
			this.addingSource = false;
		}
	}

	/**
	 * Plugins in each project's `.fluid/plugins` and `plugins` folders.
	 * Projects without any are left out; one unreadable project shows its error.
	 */
	async loadProjectPlugins(): Promise<void> {
		this.projectsLoading = true;
		this.projectsError = '';
		try {
			const projects = await api.get<Project[]>('/projects');
			const rows = await Promise.all(
				(Array.isArray(projects) ? projects : []).map(async (p): Promise<ProjectPlugins> => {
					const project = { id: p.id, name: p.name || p.id };
					try {
						return { project, plugins: await pluginsApi.getProjectPlugins(p.id) };
					} catch (err) {
						return { project, plugins: [], error: errorText(err, 'Could not read this project.') };
					}
				})
			);
			this.projectPlugins = rows.filter((r) => r.plugins.length || r.error);
			this.projectsLoaded = true;
		} catch (err) {
			this.projectsError = errorText(err, 'Could not load projects.');
		} finally {
			this.projectsLoading = false;
		}
	}

	/** Delete a plugin folder from a project. */
	removeProjectPlugin(projectId: string, pluginId: string): Promise<boolean> {
		return this.#run(
			`project:${projectId}:${pluginId}`,
			async () => {
				await pluginsApi.removeProjectPlugin(projectId, pluginId);
				this.projectPlugins = this.projectPlugins
					.map((r) => (r.project.id === projectId ? { ...r, plugins: r.plugins.filter((p) => p.id !== pluginId) } : r))
					.filter((r) => r.plugins.length || r.error);
			},
			'Could not remove plugin.'
		);
	}

	async syncSource(id: string): Promise<void> {
		this.syncing = { ...this.syncing, [id]: true };
		this.actionError = '';
		try {
			await pluginsApi.syncMarketplace(id);
			await this.load();
		} catch (err) {
			this.actionError = errorText(err, 'Could not sync source.');
		} finally {
			this.syncing = { ...this.syncing, [id]: false };
		}
	}

	async removeSource(id: string): Promise<void> {
		this.actionError = '';
		try {
			await pluginsApi.removeMarketplace(id);
			this.marketplaces = this.marketplaces.filter((m) => m.id !== id);
			await this.load();
		} catch (err) {
			this.actionError = errorText(err, 'Could not remove source.');
		}
	}

	/** Resolves false on failure (the control flips back). */
	async setAutoUpdate(patch: Partial<AutoUpdateSettings>): Promise<boolean> {
		this.actionError = '';
		try {
			this.autoUpdate = await pluginsApi.updateAutoUpdateSettings(patch);
			return true;
		} catch (err) {
			this.actionError = errorText(err, 'Could not save auto-update.');
			return false;
		}
	}

	async runAutoUpdate(): Promise<void> {
		this.checking = true;
		this.actionError = '';
		try {
			await pluginsApi.runAutoUpdate();
			await this.load();
		} catch (err) {
			this.actionError = errorText(err, 'Could not run auto-update.');
		} finally {
			this.checking = false;
		}
	}
}

const KEY = Symbol('settings-plugins');

export function setPluginsModel(model: PluginsModel): void {
	setContext(KEY, model);
}

export function getPluginsModel(): PluginsModel {
	const model = getContext<PluginsModel | undefined>(KEY);
	if (!model) throw new Error('Plugins details need the Plugins collection');
	return model;
}
