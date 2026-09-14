<script lang="ts">
	import { layout } from '$lib/stores/layout.svelte';
	import { onMount, untrack } from 'svelte';
	let { initialSelectedId }: { initialSelectedId?: string | null } = $props();
	let consumedItemId: string | null = null;
	import { api } from '$lib/api/client';
	import { Search, Plus, ArrowLeft, Settings, Package, Mail, MessageCircle, Music, House, Lightbulb, Webhook, RefreshCw } from 'lucide-svelte';
	import { getConnectors, getConnectorTypes, type Connector, type ConnectorTypeInfo } from '$lib/api/connectors';
	import { getAvailablePlugins, getInstalledPlugins, getPluginDetails, installPlugin, uninstallPlugin, enablePlugin, disablePlugin, type PluginInfo, type AvailablePlugin, type PluginDetails as Details } from '$lib/api/plugins';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import ConnectorManager from '$lib/components/ConnectorManager.svelte';
	import PluginDetails from '$lib/components/plugins/PluginDetails.svelte';
	import BuildPluginList from '$lib/components/layout/build/BuildPluginList.svelte';
	import { watchResources } from '$lib/services/watch-resources';

	type Entry = { id: string; name: string; description: string; kind: 'connection' | 'package'; installed: boolean; enabled: boolean; type?: ConnectorTypeInfo; count: number };
	let query = $state('');
	let tab = $state<'all' | 'connections' | 'skills'>('all');
	let connections: Connector[] = $state([]);
	let types: ConnectorTypeInfo[] = $state([]);
	let installed: PluginInfo[] = $state([]);
	let available: AvailablePlugin[] = $state([]);
	let selected: Entry | null = $state(null);
	let details: Details | null = $state(null);
	let manage = $state(false);
	let runtimeRefs = $state<{ id: string; name: string; active: boolean; automations: { id: string; name: string; is_enabled: boolean }[] }[]>([]);
	let pendingRemoval = $state(false);
	let loading = $state(true);
	let busy = $state('');
	let error = $state('');
	let generation = 0;
	const icons: Record<string, typeof Package> = { gmail: Mail, spotify: Music, hue: Lightbulb, homeassistant: House, webhook: Webhook };
	const descriptions: Record<string, string> = {
		gmail: 'Read mail, draft replies, and organize your inbox', spotify: 'Find music and control playback',
		discord: 'Chat with your agent in Discord', telegram: 'Chat with your agent in Telegram',
		slack: 'Work with messages and channels in Slack', whatsapp: 'Connect a WhatsApp business number',
		line: 'Connect your LINE messaging account', signal: 'Send and receive messages with Signal',
		imessage: 'Use iMessage on your Mac', bluebubbles: 'Connect iMessage through BlueBubbles',
		msteams: 'Work with messages in Microsoft Teams', hue: 'Control lights and scenes',
		homeassistant: 'Control and monitor your home', webhook: 'Send notifications and receive external events'
	};
	const entries: Entry[] = $derived([
		...types.map(t => ({ id: `connection:${t.type}`, name: t.name, description: descriptions[t.type] || t.description,
			kind: 'connection' as const, installed: !!t.plugin_installed, enabled: !!t.plugin_enabled,
			type: t, count: connections.filter(c => c.type === t.type).length })),
		...[...installed, ...available.filter(p => !installed.some(i => i.id === p.id))].filter(p => !p.connector_type).map(p => ({ id: p.id, name: p.name,
			description: p.description, kind: 'package' as const, installed: installed.some(i => i.id === p.id), enabled: installed.find(i => i.id === p.id)?.enabled ?? false,
			count: 0 }))
	]);
	const filtered = $derived(entries.filter(p => (tab === 'all' || (tab === 'connections' ? p.kind === 'connection' : p.kind === 'package')) &&
		`${p.name} ${p.description}`.toLowerCase().includes(query.trim().toLowerCase())));
	const added = $derived(entries.filter(p => p.installed || p.count > 0));
	const selectedApp = $derived.by(() => selected?.kind === 'connection' ? entries.find(e => e.id === selected?.id) : null);

	async function load() {
		const request = ++generation;
		error = '';
		try {
			const [pkgs, catalog, ct, accounts] = await Promise.all([
				getInstalledPlugins(), getAvailablePlugins(),
				$isAdmin ? getConnectorTypes() : Promise.resolve([]), $isAdmin ? getConnectors() : Promise.resolve([])
			]);
			if (request !== generation) return;
			installed = pkgs; available = catalog; types = ct; connections = accounts;
		} catch (e: any) { if (request === generation) error = e.detail || e.message || 'Could not load plugins.'; }
		finally { if (request === generation) loading = false; }
	}
	onMount(() => { void load(); return () => { generation++; }; });
	watchResources(['connectors', 'plugins'], load);

    $effect(() => {
        if (!initialSelectedId) { consumedItemId = null; return; }
        const entry = entries.find(entry => entry.id === initialSelectedId)
            ?? entries.find(entry => entry.id === `connection:${connections.find(c => c.id === initialSelectedId)?.type}`);
        if (entry && initialSelectedId !== consumedItemId) {
            consumedItemId = initialSelectedId;
            untrack(() => { void open(entry); layout.clearFeatureItemId(); });
        }
    });

	async function open(entry: Entry) {
		selected = entry; details = null; error = ''; pendingRemoval = false;
		if (entry.type?.plugin_id) await loadReferences(entry.type.plugin_id);
		if (entry.kind === 'package') {
			busy = entry.id;
			try { const result = await getPluginDetails(entry.id); if (selected?.id === entry.id) details = result; }
			catch (e: any) { error = e.detail || e.message || 'Could not load plugin details.'; }
			finally { busy = ''; }
		}
	}
	async function add(entry: Entry) {
		if (entry.installed) { await open(entry); return; }
		if (entry.kind === 'connection') { await open(entry); return; }
		busy = entry.id; error = '';
		try {
			await installPlugin(entry.id);
			await load();
			if (selected?.id === entry.id) await open({ ...entry, installed: true });
		}
		catch (e: any) { error = e.detail || e.message || 'Could not install plugin.'; }
		finally { busy = ''; }
	}
	async function changeRuntime(action: 'install' | 'enable' | 'disable' | 'uninstall' | 'rollback') {
		const entry = selectedApp;
		if (!entry?.type?.plugin_id) return;
		busy = entry.id; error = '';
		try {
			if (action === 'rollback') await api.post(`/plugins/runtime/${encodeURIComponent(entry.type.plugin_id)}/rollback`);
			else {
				const operation = { install: installPlugin, enable: enablePlugin, disable: disablePlugin, uninstall: uninstallPlugin }[action];
				await operation(entry.type.plugin_id);
			}
			pendingRemoval = false;
			await load();
			await loadReferences(entry.type.plugin_id);
		} catch (e: any) { error = e.detail || e.message || 'Could not change the connector plugin.'; }
		finally { busy = ''; }
	}
	async function loadReferences(id: string) {
		try { runtimeRefs = await api.get(`/plugins/runtime/${encodeURIComponent(id)}/connections`); }
		catch (e: any) { error = e.detail || 'Could not load plugin dependencies.'; runtimeRefs = []; }
	}
	function back() { selected = null; details = null; manage = false; void load(); }
</script>

<section class="plugin-hub" aria-label="Plugins">
	{#if selected || manage}
		<button class="back" onclick={back}><ArrowLeft size={16} /> All plugins</button>
	{/if}
	{#if error}<p class="error" role="alert">{error} <button onclick={load}>Retry</button></p>{/if}
	{#if manage}
		<BuildPluginList />
	{:else if selected?.kind === 'connection' && selected.type}
		<header class="detail-head"><h2>{selected.name}</h2><p>{selected.description}</p><span class="muted">Official connector plugin · Installed on this host</span></header>
		<div class="runtime-controls">
			{#if !selectedApp?.installed}
				<p>Download the official connector runtime. Its code is pinned and integrity-checked, and runs in its own process and dependency environment, with AI Shuffle's OS permissions. Only first-party runtimes are supported. This distribution uses a private GitHub source; sign in to GitHub in Settings with repository access before installing.</p>
				{#if selectedApp?.count}<p>Your existing connections keep working with the bundled runtime. Installing preserves their IDs and automations; running connections use the downloaded runtime after their next restart.</p>{/if}
				<button disabled={!!busy} onclick={() => changeRuntime('install')}>{busy ? 'Installing runtime and dependencies…' : `Install ${selected.name}`}</button>
			{:else}
				<p>{selectedApp.enabled ? 'Runtime installed and enabled.' : 'Runtime installed but disabled.'} Remove dependent connections before disabling or uninstalling; automation references protect those connections too.</p>
				<button disabled={!!busy} onclick={() => changeRuntime(selectedApp?.enabled ? 'disable' : 'enable')}>{selectedApp.enabled ? 'Disable runtime' : 'Enable runtime'}</button>
				<button disabled={!!busy} onclick={() => pendingRemoval = true}>Uninstall runtime</button>
				{#if selectedApp.type?.plugin_update_available}<button disabled={!!busy} onclick={() => changeRuntime('install')}>Update runtime</button>{/if}
				{#if selectedApp.type?.plugin_rollback_available}<button disabled={!!busy} onclick={() => changeRuntime('rollback')}>Roll back runtime</button>{/if}
				<p>Version {selectedApp.type?.plugin_version}. Pause dependent automations and disable connections before updating or rolling back. Account IDs and settings are kept.</p>
				{#if pendingRemoval}
					<div role="alert"><p>Uninstall this runtime? Connected accounts must be removed first. Downloaded versions remain cached on this host.</p><button disabled={!!busy} onclick={() => changeRuntime('uninstall')}>Confirm uninstall</button><button onclick={() => pendingRemoval = false}>Cancel</button></div>
				{/if}
			{/if}
			{#if runtimeRefs.length}
				<details><summary>Used by {runtimeRefs.length} connection{runtimeRefs.length === 1 ? '' : 's'}</summary>
					<ul>{#each runtimeRefs as connection}<li>{connection.name} ({connection.active ? 'active' : 'inactive'}){#if connection.automations.length}<ul>{#each connection.automations as automation}<li>{automation.name} ({automation.is_enabled ? 'enabled' : 'paused'} automation)</li>{/each}</ul>{/if}</li>{/each}</ul>
				</details>
			{/if}
		</div>
		{#if selectedApp?.enabled || selectedApp?.count}
			{#key `${selected.id}:${selectedApp?.installed}`}<ConnectorManager typeFilter={selected.type.type} onChanged={() => { void load(); if (selected?.type?.plugin_id) void loadReferences(selected.type.plugin_id); }} />{/key}
		{/if}
	{:else if selected && details}
		<PluginDetails plugin={details} onClose={back} onInstall={() => { if (selected) void add(selected); }} installing={busy === selected.id} />
	{:else if selected}
		<p role="status">{busy ? 'Loading plugin…' : 'Plugin details are unavailable.'}</p>
	{:else}
		<header class="hub-header"><div><h2>Plugins</h2><p>Connect your apps and add skills to your agents.</p></div>
			<div class="header-actions"><button class="icon-button" aria-label="Refresh plugins" onclick={load} disabled={loading}><RefreshCw size={17} /></button>
				{#if $isAdmin}<button class="icon-button" aria-label="Manage plugins and sources" onclick={() => manage = true}><Settings size={18} /></button>{/if}</div>
		</header>
		<label class="search"><Search size={17} /><input aria-label="Search plugins" placeholder="Search plugins" bind:value={query} /></label>
		{#if added.length}
			<div class="section-label"><h3>Installed & connected</h3><span>{added.length}</span></div>
			<div class="installed-strip" aria-label="Installed and connected plugins">
				{#each added as entry (entry.id)}
					{@const Icon = entry.type ? (icons[entry.type.type] || MessageCircle) : Package}
					<button class="installed-item" onclick={() => open(entry)} title={entry.name} aria-label={`Manage ${entry.name}`}><span class="plugin-icon"><Icon size={22} /></span><span>{entry.name}</span></button>
				{/each}
			</div>
		{/if}
		<div class="filters" aria-label="Plugin categories">
			{#each [{ id: 'all', label: 'All' }, { id: 'connections', label: 'Apps' }, { id: 'skills', label: 'Agent plugins' }] as category}
				<button class:active={tab === category.id} aria-pressed={tab === category.id} onclick={() => tab = category.id as typeof tab}>{category.label}</button>
			{/each}
		</div>
		<div class="section-label"><h3>{query ? 'Search results' : 'Explore plugins'}</h3><span>{filtered.length}</span></div>
		{#if loading}<p role="status">Loading plugins…</p>
		{:else if !filtered.length}<p class="empty">{query ? 'No plugins match your search.' : 'No plugins here yet. Add a marketplace in plugin settings.'}</p>
		{:else}<div class="catalog">
			{#each filtered as entry (entry.id)}
				{@const Icon = entry.type ? (icons[entry.type.type] || MessageCircle) : Package}
				<article class="plugin-row">
					<button class="entry" onclick={() => open(entry)}><span class="plugin-icon"><Icon size={23} /></span><span class="entry-copy"><strong>{entry.name}</strong><span>{entry.description}</span><small>{entry.kind === 'connection' ? (entry.count ? `${entry.count} connection${entry.count === 1 ? '' : 's'}` : entry.installed ? (entry.enabled ? 'Installed' : 'Disabled') : 'Official plugin') : entry.installed ? (entry.enabled ? 'Installed' : 'Disabled') : 'Marketplace plugin'}</small></span></button>
					{#if $isAdmin}<button class="add" aria-label={`${entry.installed || entry.count ? 'Manage' : 'Install'} ${entry.name}`} disabled={!!busy} onclick={() => add(entry)}>{#if busy === entry.id}<span>…</span>{:else if entry.installed}<Settings size={17} />{:else}<Plus size={20} />{/if}</button>{/if}
				</article>
			{/each}
		</div>{/if}
	{/if}
</section>

<style>
	.runtime-controls { margin-bottom: 1rem; padding: 1rem; border: 1px solid var(--border); border-radius: .6rem; }
	.runtime-controls p { margin: .5rem 0; color: var(--muted-foreground); font-size: .85rem; line-height: 1.5; }
	.runtime-controls button { border: 1px solid var(--border); border-radius: .4rem; padding: .4rem .65rem; margin: .25rem .4rem .25rem 0; }
	.runtime-controls ul { padding-left: 1rem; font-size: .85rem; }
	.plugin-hub { max-width: 1040px; margin: 0 auto; width: 100%; color: var(--foreground); }
	.hub-header, .header-actions, .section-label { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
	h2 { font-size: 24px; font-weight: 650; margin: 0 0 6px; } h3 { font-size: 14px; font-weight: 600; margin: 0; }
	p { margin: 0; } .hub-header p, .detail-head p, .muted, .empty { color: var(--muted-foreground); font-size: 13px; }
	.hub-header { margin-bottom: 24px; } .detail-head { margin: 12px 0 24px; } .detail-head p { margin-bottom: 8px; }
	button { cursor: pointer; } button:disabled { opacity: .5; cursor: wait; }
	button:focus-visible, input:focus-visible { outline: 2px solid var(--ring); outline-offset: 3px; }
	.search { display: flex; align-items: center; gap: 10px; border: 1px solid var(--border); border-radius: 24px; padding: 9px 14px; background: var(--secondary); color: var(--muted-foreground); margin-bottom: 28px; }
	.search input { width: 100%; min-width: 0; background: transparent; border: 0; color: var(--foreground); font-size: 14px; }
	.section-label { padding-bottom: 12px; border-bottom: 1px solid var(--border); margin: 22px 0 12px; } .section-label > span { color: var(--muted-foreground); font-size: 12px; }
	.installed-strip { display: flex; gap: 10px; padding: 6px 0 12px; overflow-x: auto; }
	.installed-item { display: flex; flex-direction: column; align-items: center; gap: 7px; flex: 0 0 72px; min-width: 0; font-size: 11px; } .installed-item > span:last-child { width: 72px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.plugin-icon { width: 40px; height: 40px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: 10px; background: var(--secondary); flex-shrink: 0; }
	.filters { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 18px; } .filters button { padding: 7px 12px; border-radius: 8px; font-size: 13px; color: var(--muted-foreground); } .filters button.active { background: var(--secondary); color: var(--foreground); }
	.catalog { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 24px; row-gap: 6px; }
	.plugin-row { display: flex; align-items: center; gap: 6px; min-width: 0; padding: 8px; border-radius: 10px; } .plugin-row:hover { background: var(--secondary); }
	.entry { display: flex; align-items: center; gap: 12px; text-align: left; flex: 1; min-width: 0; padding: 3px 0; } .entry-copy { display: flex; flex-direction: column; gap: 4px; min-width: 0; } .entry-copy strong { font-size: 14px; font-weight: 600; } .entry-copy > span { font-size: 12px; color: var(--muted-foreground); line-height: 1.45; } .entry-copy small { font-size: 10px; color: var(--muted-foreground); }
	.add, .icon-button { display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: 8px; } .add:hover, .icon-button:hover { background: var(--muted); }
	.back { display: flex; gap: 8px; align-items: center; font-size: 13px; margin-bottom: 20px; padding: 8px 0; } .error { color: var(--destructive); margin: 12px 0; font-size: 13px; } .error button { text-decoration: underline; } .empty { padding: 20px 0; }
	@media (max-width: 760px) { .catalog { grid-template-columns: 1fr; } .hub-header { align-items: flex-start; } .plugin-row { padding-left: 0; padding-right: 0; } .add, .icon-button { width: 44px; height: 44px; } }
</style>
