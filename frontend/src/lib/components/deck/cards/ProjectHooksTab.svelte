<script lang="ts">
	/**
	 * ProjectHooksTab - Hook list tab for project detail view
	 *
	 * Read-only list of custom and plugin hooks.
	 * Supports toggle, test, and delete for custom hooks.
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
	import type { Hook, HookUpdate, HookTestResult } from '$lib/api/client';
	import HookListItem from '$lib/components/hooks/HookListItem.svelte';
	import PluginHookItem from '$lib/components/hooks/PluginHookItem.svelte';
	import { Search, X, Loader2, Zap, Package } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Tab state
	type Tab = 'custom' | 'plugins';
	let activeTab = $state<Tab>('custom');
	let searchQuery = $state('');

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
</script>

<div class="hooks-tab">
	{#if $hooksLoading}
		<div class="card-empty-state">
			<Loader2 class="card-empty-icon animate-spin" />
			<p class="card-empty-title">Loading hooks...</p>
		</div>
	{:else if $hooksError}
		<div class="card-error-banner">
			<span class="flex-1">{$hooksError}</span>
			<button onclick={() => hooks.clearError()}>
				<X class="w-4 h-4" />
			</button>
		</div>
	{:else}
		<!-- Tabs -->
		<div class="tab-bar">
			<button
				class="tab-btn"
				class:active={activeTab === 'custom'}
				onclick={() => (activeTab = 'custom')}
			>
				<Zap class="w-4 h-4" />
				Custom
				{#if $hookStats.totalCustom > 0}
					<span class="tab-badge">{$hookStats.totalCustom}</span>
				{/if}
			</button>
			<button
				class="tab-btn"
				class:active={activeTab === 'plugins'}
				onclick={() => (activeTab = 'plugins')}
			>
				<Package class="w-4 h-4" />
				Plugins
				{#if $hookStats.totalPlugin > 0}
					<span class="tab-badge">{$hookStats.totalPlugin}</span>
				{/if}
			</button>
		</div>

		<!-- Search -->
		{#if ($projectHooks.length > 3 && activeTab === 'custom') || ($pluginHooks.length > 3 && activeTab === 'plugins')}
			<div class="tab-search">
				<div class="card-search-container">
					<Search class="card-search-icon" />
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search hooks..."
						class="card-search-input"
					/>
				</div>
			</div>
		{/if}

		<!-- Test result toast -->
		{#if testResult}
			<div class="test-toast" class:success={testResult.result.success} class:error={!testResult.result.success}>
				{#if testResult.result.success}
					<span>Test passed ({testResult.result.duration_ms}ms)</span>
				{:else}
					<span>Test failed: {testResult.result.error}</span>
				{/if}
				<button onclick={() => (testResult = null)}>
					<X class="w-4 h-4" />
				</button>
			</div>
		{/if}

		<!-- Hook list -->
		<div class="list-view">
			{#if activeTab === 'custom'}
				{#if filteredHooks().length === 0}
					<div class="card-empty-state">
						<Zap class="card-empty-icon" />
						<p class="card-empty-title">No custom hooks</p>
						<p class="card-empty-description">
							Create hooks from the Hooks card to automate actions on tool use events.
						</p>
					</div>
				{:else}
					{#each filteredHooks() as hook, index (hook.id)}
						{#if index > 0}<div class="fp-list-divider"></div>{/if}
						<HookListItem
							{hook}
							testing={$hookTesting[hook.id] || false}
							saving={$hookSaving[hook.id] || false}
							onEdit={() => {}}
							onDelete={() => handleDeleteHook(hook.id)}
							onToggle={() => handleToggleHook(hook.id, !hook.enabled)}
							onTest={() => handleTestHook(hook.id)}
						/>
					{/each}
				{/if}
			{:else}
				{#if filteredPluginHooks().length === 0}
					<div class="card-empty-state">
						<Package class="card-empty-icon" />
						<p class="card-empty-title">No plugin hooks</p>
						<p class="card-empty-description">
							Install plugins with hooks to see them here.
						</p>
					</div>
				{:else}
					{#each filteredPluginHooks() as hook, index (hook.plugin_id + hook.event)}
						{#if index > 0}<div class="fp-list-divider"></div>{/if}
						<PluginHookItem {hook} />
					{/each}
				{/if}
			{/if}
		</div>
	{/if}
</div>

<style>
	.hooks-tab {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.tab-bar {
		display: flex;
		gap: var(--space-1);
		padding: var(--space-2) var(--space-4);
		border-bottom: 1px solid var(--border);
		background: var(--muted);
	}

	.tab-btn {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
		border-radius: var(--radius);
		background: transparent;
		border: none;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.tab-btn:hover {
		color: var(--foreground);
	}

	.tab-btn.active {
		background: var(--background);
		color: var(--foreground);
		font-weight: 500;
	}

	.tab-badge {
		font-size: var(--text-xs);
		padding: 0 var(--space-1);
		border-radius: var(--radius-full);
		background: var(--muted);
	}

	.tab-btn.active .tab-badge {
		background: var(--selection);
		color: var(--primary-foreground);
	}

	.tab-search {
		padding: var(--space-2) var(--space-4);
	}

	.list-view {
		flex: 1;
		overflow-y: auto;
		padding: 0 var(--space-4);
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.test-toast {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--space-2) var(--space-4);
		margin: 0 var(--space-4);
		border-radius: var(--radius);
		font-size: var(--text-sm);
	}

	.test-toast.success {
		background: color-mix(in oklch, var(--success) 20%, transparent);
		color: var(--success);
	}

	.test-toast.error {
		background: color-mix(in oklch, var(--destructive) 20%, transparent);
		color: var(--destructive);
	}

	.flex-1 {
		flex: 1;
	}

</style>
