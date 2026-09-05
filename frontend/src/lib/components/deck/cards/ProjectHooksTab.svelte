<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * ProjectHooksTab - Hook list tab for project detail view
	 *
	 * Studio-v2 grouped-field design (.set-group / .set-row), mirroring the
	 * Profile editor. Field primitives come from project-tabs.css (.ptab).
	 *
	 * Custom + plugin hooks. Custom hooks support toggle, test, and delete.
	 */
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
	import HookListItem from '$lib/components/hooks/HookListItem.svelte';
	import PluginHookItem from '$lib/components/hooks/PluginHookItem.svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { Search, Zap, Package } from 'lucide-svelte';
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
			hooks.loadAll(projectId);
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

<div class="ptab">
	{#if $hooksLoading}
		<div class="ptab-loading"><div class="ptab-spinner"></div></div>
	{:else if $hooksError}
		<Banner severity="error" message={$hooksError} dismissible onDismiss={() => hooks.clearError()} />
	{:else}
		<!-- Custom / Plugins segmented control -->
		<div class="hooks-head">
			<div class="seg">
				<button class:on={activeTab === 'custom'} onclick={() => (activeTab = 'custom')}>
					<Zap />Custom{#if $hookStats.totalCustom > 0} ({$hookStats.totalCustom}){/if}
				</button>
				<button class:on={activeTab === 'plugins'} onclick={() => (activeTab = 'plugins')}>
					<Package />Plugins{#if $hookStats.totalPlugin > 0} ({$hookStats.totalPlugin}){/if}
				</button>
			</div>
		</div>

		<!-- Search -->
		{#if showSearch}
			<div class="tsearch">
				<Search />
				<input type="text" bind:value={searchQuery} placeholder="Search hooks…" class="inp" />
			</div>
		{/if}

		<!-- Test result -->
		{#if testResult}
			<div class="test-banner">
				<Banner
					severity={testResult.result.success ? 'success' : 'error'}
					message={testResult.result.success
						? `Test passed (${testResult.result.duration_ms}ms)`
						: `Test failed: ${testResult.result.error}`}
					dismissible
					onDismiss={() => (testResult = null)}
				/>
			</div>
		{/if}

		<!-- Hook list -->
		{#if activeTab === 'custom'}
			{#if filteredHooks().length === 0}
				<div class="empty-state">
					<Zap />
					<p class="es-title">No custom hooks</p>
					<p class="es-desc">Create hooks from the Hooks card to automate actions on tool-use events.</p>
				</div>
			{:else}
				<div class="set-group">
					<div class="set-grouphead"><h3><Zap />Custom hooks</h3></div>
					{#each filteredHooks() as hook (hook.id)}
						<HookListItem
							{hook}
							testing={$hookTesting[hook.id] || false}
							saving={$hookSaving[hook.id] || false}
							menuOpen={actionsOpenId === hook.id}
							onToggleMenu={(e) => toggleRowActions(e, hook.id)}
							onDelete={() => handleDeleteHook(hook.id)}
							onToggle={() => handleToggleHook(hook.id, !hook.enabled)}
							onTest={() => { actionsOpenId = null; handleTestHook(hook.id); }}
						/>
					{/each}
				</div>
			{/if}
		{:else}
			{#if filteredPluginHooks().length === 0}
				<div class="empty-state">
					<Package />
					<p class="es-title">No plugin hooks</p>
					<p class="es-desc">Install plugins with hooks to see them here.</p>
				</div>
			{:else}
				<div class="set-group">
					<div class="set-grouphead"><h3><Package />Plugin hooks</h3></div>
					{#each filteredPluginHooks() as hook (hook.plugin_id + hook.event)}
						<PluginHookItem {hook} />
					{/each}
				</div>
			{/if}
		{/if}
	{/if}
</div>

<style>
	.hooks-head {
		margin-bottom: 16px;
	}

	.test-banner {
		margin-bottom: 16px;
	}
</style>
