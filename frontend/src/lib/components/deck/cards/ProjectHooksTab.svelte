<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/** Custom and plugin hooks using the shared Settings row grammar. */
	import {
		hooks,
		projectHooks,
		pluginHooks,
		hooksLoading,
		hooksError,
		hookTesting,
		hookSaving,
		hookStats
	} from '$lib/stores/hooks.svelte';
	import type { HookTestResult } from '$lib/api/client';
	import { RowGroup, SettingRow, PanelSearch, EmptyDetail, InlineBanner, Segmented, Switch } from '$lib/components/layout/view';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import { Play, Trash2 } from 'lucide-svelte';
	import './project-tabs.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Tab state
	type Tab = 'custom' | 'plugins';
	let activeTab = $state<Tab>('custom');
	let searchQuery = $state('');

	// Touch/narrow only: which custom-hook row has its collapsed ⋮ tray expanded
	// (one at a time; desktop keeps the always-inline hover rail). The ⋮ toggle
	// and tray buttons stop propagation, so any click reaching the window is
	// outside an open tray — collapse it.
	let actionsOpenId = $state<string | null>(null);
	function toggleRowActions(e: MouseEvent | KeyboardEvent, id: string) {
		e.stopPropagation();
		actionsOpenId = actionsOpenId === id ? null : id;
	}
	function onWindowClick() {
		if (actionsOpenId !== null) actionsOpenId = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') actionsOpenId = null;
	}

	// Test result
	let testResult = $state<{ hookId: string; result: HookTestResult } | null>(null);

	// Load hooks when project changes
	$effect(() => {
		if (projectId) {
			// A failed load is shown through $hooksError.
			hooks.loadAll(projectId).catch(() => {});
		}
	});

	// Filter custom hooks
	const filteredHooks = $derived(() => {
		let items = $projectHooks;
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			items = items.filter(
				(h) =>
					h.name.toLowerCase().includes(query) ||
					h.event.toLowerCase().includes(query) ||
					h.command?.toLowerCase().includes(query) ||
					h.url?.toLowerCase().includes(query)
			);
		}
		return items;
	});

	// Filter plugin hooks
	const filteredPluginHooks = $derived(() => {
		let items = $pluginHooks;
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			items = items.filter(
				(h) =>
					h.plugin_name.toLowerCase().includes(query) ||
					h.event.toLowerCase().includes(query) ||
					h.description?.toLowerCase().includes(query)
			);
		}
		return items;
	});

	// Handle hook deletion
	async function handleDeleteHook(hookId: string) {
		try {
			await hooks.deleteHook(hookId);
		} catch (e) {
			console.error('Failed to delete hook:', e);
		}
	}

	// Handle hook toggle
	async function handleToggleHook(hookId: string, enabled: boolean) {
		try {
			await hooks.updateHook(hookId, { enabled });
		} catch (e) {
			console.error('Failed to toggle hook:', e);
		}
	}

	// Handle hook test
	async function handleTestHook(hookId: string) {
		try {
			const result = await hooks.testHook(hookId);
			testResult = { hookId, result };
			setTimeout(() => {
				if (testResult?.hookId === hookId) {
					testResult = null;
				}
			}, 5000);
		} catch (e) {
			console.error('Failed to test hook:', e);
		}
	}

	const showSearch = $derived(
		($projectHooks.length > 3 && activeTab === 'custom') ||
		($pluginHooks.length > 3 && activeTab === 'plugins')
	);
	watchResources(["hooks", "plugins"], () => projectId ? hooks.loadAll(projectId) : undefined);
</script>
<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />
{#if $hooksLoading}<p class="project-muted" role="status">Loading hooks…</p>
{:else if $hooksError}<InlineBanner>{$hooksError}{#snippet actions()}<button class="st-btn" onclick={() => hooks.clearError()}>Dismiss</button>{/snippet}</InlineBanner>
{:else}
	<div class="project-toolbar">
		<Segmented label="Hook source" size="sm" bind:value={activeTab} options={[{ value: 'custom', label: `Custom${$hookStats.totalCustom ? ` (${$hookStats.totalCustom})` : ''}` }, { value: 'plugins', label: `Plugins${$hookStats.totalPlugin ? ` (${$hookStats.totalPlugin})` : ''}` }]} />
	</div>
	{#if showSearch}<PanelSearch bind:value={searchQuery} label="Search hooks" placeholder="Search hooks" />{/if}
	{#if testResult}<InlineBanner tone={testResult.result.success ? 'info' : 'warn'}>{testResult.result.success ? `Test passed (${testResult.result.duration_ms}ms)` : `Test failed: ${testResult.result.error}`}{#snippet actions()}<button class="st-btn sm" onclick={() => (testResult = null)}>Dismiss</button>{/snippet}</InlineBanner>{/if}
	{#if activeTab === 'custom'}
		{#if !filteredHooks().length}
			<EmptyDetail title="No custom hooks" message="Create hooks from the Hooks card to automate actions on tool-use events.">{#if searchQuery}<button class="st-btn" onclick={() => (searchQuery = '')}>Clear search</button>{/if}</EmptyDetail>
		{:else}
			<RowGroup title="Custom hooks">
				{#each filteredHooks() as hook (hook.id)}
					<SettingRow label={hook.name} hint={[hook.event, hook.hook_type, hook.tool_matcher, hook.failure_count ? `${hook.failure_count} failures` : ''].filter(Boolean).join(' · ')} dim={!hook.enabled}>
						{#snippet expanded()}{#if hook.command || hook.url}<p class="project-code">{hook.command ?? hook.url}</p>{/if}{/snippet}
						<RowActionMenu open={actionsOpenId === hook.id} onToggle={(e) => toggleRowActions(e, hook.id)} label="Hook actions">
							<button class="st-btn icon" title="Test hook" aria-label="Test hook" onclick={(e) => { e.stopPropagation(); actionsOpenId = null; handleTestHook(hook.id); }} disabled={$hookTesting[hook.id] || $hookSaving[hook.id]}><Play /></button>
							<button class="st-btn icon danger" title="Delete hook" aria-label="Delete hook" onclick={(e) => { e.stopPropagation(); handleDeleteHook(hook.id); }} disabled={$hookSaving[hook.id]}><Trash2 /></button>
						</RowActionMenu>
						<Switch controlled size="sm" checked={hook.enabled} label={hook.enabled ? 'Disable hook' : 'Enable hook'} title={hook.enabled ? 'Disable hook' : 'Enable hook'} disabled={$hookSaving[hook.id]} onchange={() => handleToggleHook(hook.id, !hook.enabled)} />
					</SettingRow>
				{/each}
			</RowGroup>
		{/if}
	{:else}
		{#if !filteredPluginHooks().length}<EmptyDetail title="No plugin hooks" message="Install plugins with hooks to see them here.">{#if searchQuery}<button class="st-btn" onclick={() => (searchQuery = '')}>Clear search</button>{/if}</EmptyDetail>
		{:else}<RowGroup title="Plugin hooks">{#each filteredPluginHooks() as hook (hook.plugin_id + hook.event)}<SettingRow label={hook.plugin_name} hint={[hook.event, hook.hook_type, hook.tool_matcher, hook.description].filter(Boolean).join(' · ')} />{/each}</RowGroup>{/if}
	{/if}
{/if}
