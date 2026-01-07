<script lang="ts">
	/**
	 * HooksCard - Hook management card for The Deck
	 *
	 * Features:
	 * - View and manage custom hooks per project
	 * - View plugin-contributed hooks (read-only)
	 * - Create, edit, delete, and test hooks
	 * - Enable/disable hooks
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { onMount } from 'svelte';
	import {
		hooks,
		projectHooks,
		pluginHooks,
		hooksLoading,
		hooksError,
		hookTesting,
		hookSaving,
		hookStats
	} from '$lib/stores/hooks';
	import { selectedProject, projects } from '$lib/stores/chat';
	import type { Hook, HookCreate, HookUpdate, HookTestResult } from '$lib/api/client';
	import HookListItem from '$lib/components/hooks/HookListItem.svelte';
	import HookForm from '$lib/components/hooks/HookForm.svelte';
	import PluginHookItem from '$lib/components/hooks/PluginHookItem.svelte';
	import { Search, X, Loader2, Zap, Plus, AlertCircle, FolderKanban, CheckCircle, XCircle } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	type Tab = 'custom' | 'plugins';
	let activeTab = $state<Tab>('custom');
	let searchQuery = $state('');

	// Form state
	let showForm = $state(false);
	let editingHook = $state<Hook | null>(null);
	let formSaving = $state(false);

	// Test result display
	let testResult = $state<{ hookId: string; result: HookTestResult } | null>(null);

	// Get current project
	const currentProject = $derived($projects.find(p => p.id === $selectedProject));
	const hasProject = $derived(!!$selectedProject && !!currentProject);

	// Load data when project changes
	$effect(() => {
		if ($selectedProject) {
			hooks.loadAll($selectedProject);
		} else {
			hooks.reset();
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

	// Handle hook creation
	async function handleCreateHook(data: HookCreate) {
		if (!$selectedProject) return;

		formSaving = true;
		try {
			await hooks.createHook($selectedProject, data);
			showForm = false;
			editingHook = null;
		} catch (e) {
			console.error('Failed to create hook:', e);
		} finally {
			formSaving = false;
		}
	}

	// Handle hook update
	async function handleUpdateHook(data: HookUpdate) {
		if (!editingHook) return;

		formSaving = true;
		try {
			await hooks.updateHook(editingHook.id, data);
			showForm = false;
			editingHook = null;
		} catch (e) {
			console.error('Failed to update hook:', e);
		} finally {
			formSaving = false;
		}
	}

	// Handle form submission
	function handleFormSave(data: HookCreate | HookUpdate) {
		if (editingHook) {
			handleUpdateHook(data as HookUpdate);
		} else {
			handleCreateHook(data as HookCreate);
		}
	}

	// Handle hook test
	async function handleTestHook(hookId: string) {
		try {
			const result = await hooks.testHook(hookId);
			testResult = { hookId, result };
			// Auto-clear after 5 seconds
			setTimeout(() => {
				if (testResult?.hookId === hookId) {
					testResult = null;
				}
			}, 5000);
		} catch (e) {
			console.error('Failed to test hook:', e);
		}
	}

	// Handle delete
	async function handleDeleteHook(hookId: string) {
		if (!confirm('Are you sure you want to delete this hook?')) return;
		try {
			await hooks.deleteHook(hookId);
		} catch (e) {
			console.error('Failed to delete hook:', e);
		}
	}

	// Tab definitions
	const tabs = $derived([
		{ id: 'custom' as Tab, label: 'Custom Hooks', count: $hookStats.totalCustom },
		{ id: 'plugins' as Tab, label: 'Plugin Hooks', count: $hookStats.totalPlugin }
	]);
</script>

<BaseCard
	{card}
	{onClose}
	{onMaximize}
	{onFocus}
	{onMove}
	{onResize}
	{onDragEnd}
	{onResizeEnd}
>
	{#snippet children()}
		<div class="hooks-card" class:mobile class:maximized={card.maximized}>
			<!-- No project selected state -->
			{#if !hasProject}
				<div class="no-project-state">
					<div class="empty-icon">
						<FolderKanban size={32} />
					</div>
					<h3>No Project Selected</h3>
					<p>Select a project to manage its hooks</p>
				</div>
			{:else}
				<!-- Tabs -->
				<div class="hooks-tabs">
					{#each tabs as tab}
						<button
							onclick={() => activeTab = tab.id}
							class="hooks-tab"
							class:active={activeTab === tab.id}
						>
							{tab.label}
							<span class="hooks-tab-count" class:active={activeTab === tab.id}>
								{tab.count}
							</span>
						</button>
					{/each}
				</div>

				<!-- Search bar and actions -->
				<div class="hooks-search-bar">
					<div class="search-input-wrapper">
						<Search size={16} class="search-icon" />
						<input
							type="text"
							bind:value={searchQuery}
							placeholder="Search hooks..."
							class="search-input"
						/>
					</div>

					{#if activeTab === 'custom'}
						<button
							onclick={() => {
								editingHook = null;
								showForm = true;
							}}
							class="add-hook-btn"
						>
							<Plus size={16} />
							Add Hook
						</button>
					{/if}
				</div>

				<!-- Test result toast -->
				{#if testResult}
					<div class="test-result" class:success={testResult.result.success} class:error={!testResult.result.success}>
						{#if testResult.result.success}
							<CheckCircle size={16} />
							<span>Hook test successful ({testResult.result.duration_ms}ms)</span>
						{:else}
							<XCircle size={16} />
							<span>Hook test failed: {testResult.result.error}</span>
						{/if}
						<button onclick={() => testResult = null} class="test-result-close">
							<X size={14} />
						</button>
					</div>
				{/if}

				<!-- Error display -->
				{#if $hooksError}
					<div class="hooks-error">
						<AlertCircle size={16} />
						<span>{$hooksError}</span>
						<button onclick={() => hooks.clearError()} class="error-close">
							<X size={14} />
						</button>
					</div>
				{/if}

				<!-- Content -->
				<div class="hooks-content">
					{#if $hooksLoading}
						<div class="hooks-loading">
							<Loader2 size={32} class="animate-spin" />
							<p>Loading hooks...</p>
						</div>
					{:else if activeTab === 'custom'}
						<div class="hooks-list">
							{#if filteredHooks().length === 0}
								<div class="empty-state">
									<div class="empty-icon">
										<Zap size={24} />
									</div>
									<p>No custom hooks</p>
									<button
										onclick={() => {
											editingHook = null;
											showForm = true;
										}}
										class="add-first-hook-btn"
									>
										Add your first hook
									</button>
								</div>
							{:else}
								<div class="hook-items">
									{#each filteredHooks() as hook (hook.id)}
										<HookListItem
											{hook}
											testing={$hookTesting[hook.id] || false}
											saving={$hookSaving[hook.id] || false}
											onToggle={() => hooks.toggleHook(hook.id)}
											onEdit={() => {
												editingHook = hook;
												showForm = true;
											}}
											onDelete={() => handleDeleteHook(hook.id)}
											onTest={() => handleTestHook(hook.id)}
										/>
									{/each}
								</div>
							{/if}
						</div>
					{:else}
						<div class="hooks-list">
							{#if filteredPluginHooks().length === 0}
								<div class="empty-state">
									<div class="empty-icon">
										<Zap size={24} />
									</div>
									<p>No plugin hooks installed</p>
									<p class="empty-hint">Install plugins with hooks to see them here</p>
								</div>
							{:else}
								<div class="hook-items">
									{#each filteredPluginHooks() as hook (hook.plugin_id + hook.event)}
										<PluginHookItem {hook} />
									{/each}
								</div>
							{/if}
						</div>
					{/if}
				</div>

				<!-- Footer -->
				<div class="hooks-footer">
					<div class="footer-info">
						<FolderKanban size={14} />
						{currentProject?.name || 'Unknown Project'}
					</div>
					<div class="footer-stats">
						{$hookStats.totalEnabled} enabled / {$hookStats.totalCustom} custom
					</div>
				</div>
			{/if}
		</div>

		<!-- Add/Edit Hook Form Modal -->
		{#if showForm}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<div
				class="form-overlay"
				onclick={(e) => e.target === e.currentTarget && (showForm = false)}
			>
				<HookForm
					hook={editingHook}
					saving={formSaving}
					onSave={handleFormSave}
					onCancel={() => {
						showForm = false;
						editingHook = null;
					}}
				/>
			</div>
		{/if}
	{/snippet}
</BaseCard>

<style>
	.hooks-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	/* Maximized state - constrain width and center content */
	.hooks-card.maximized {
		--card-max-width: 800px;
	}

	.hooks-card.maximized .hooks-tabs,
	.hooks-card.maximized .hooks-search-bar,
	.hooks-card.maximized .hooks-content,
	.hooks-card.maximized .hooks-footer,
	.hooks-card.maximized .test-result,
	.hooks-card.maximized .hooks-error {
		max-width: var(--card-max-width);
		margin-left: auto;
		margin-right: auto;
		width: 100%;
	}

	/* No project state */
	.no-project-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px 24px;
		text-align: center;
		color: var(--muted-foreground);
		height: 100%;
	}

	.no-project-state h3 {
		font-size: 1rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 16px 0 8px;
	}

	.no-project-state p {
		font-size: 0.875rem;
		margin: 0;
	}

	/* Tabs */
	.hooks-tabs {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 8px 16px;
		border-bottom: 1px solid var(--border);
		background: var(--secondary);
		overflow-x: auto;
		flex-shrink: 0;
	}

	.hooks-tab {
		padding: 6px 12px;
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: 6px;
		white-space: nowrap;
		display: flex;
		align-items: center;
		gap: 6px;
		color: var(--muted-foreground);
		background: transparent;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.hooks-tab:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.hooks-tab.active {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.hooks-tab-count {
		padding: 1px 6px;
		font-size: 0.6875rem;
		border-radius: 9999px;
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.hooks-tab-count.active {
		background: color-mix(in oklch, var(--primary-foreground) 20%, transparent);
		color: var(--primary-foreground);
	}

	/* Search bar */
	.hooks-search-bar {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.search-input-wrapper {
		position: relative;
		flex: 1;
	}

	.search-input-wrapper :global(.search-icon) {
		position: absolute;
		left: 12px;
		top: 50%;
		transform: translateY(-50%);
		color: var(--muted-foreground);
	}

	.search-input {
		width: 100%;
		padding: 8px 12px 8px 36px;
		border-radius: 6px;
		background: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.search-input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.add-hook-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 12px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 6px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.add-hook-btn:hover {
		filter: brightness(1.1);
	}

	/* Test result toast */
	.test-result {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 16px;
		padding: 10px 12px;
		border-radius: 6px;
		font-size: 0.8125rem;
		animation: slideDown 200ms ease;
	}

	.test-result.success {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border: 1px solid color-mix(in oklch, var(--success) 30%, transparent);
	}

	.test-result.error {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.test-result-close {
		margin-left: auto;
		padding: 2px;
		background: transparent;
		border: none;
		cursor: pointer;
		opacity: 0.7;
		transition: opacity 150ms ease;
	}

	.test-result-close:hover {
		opacity: 1;
	}

	@keyframes slideDown {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	/* Error display */
	.hooks-error {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 12px 16px 0;
		padding: 10px 12px;
		border-radius: 6px;
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		font-size: 0.8125rem;
	}

	.error-close {
		margin-left: auto;
		padding: 4px;
		background: transparent;
		border: none;
		color: var(--destructive);
		cursor: pointer;
		border-radius: 4px;
		transition: opacity 150ms ease;
	}

	.error-close:hover {
		opacity: 0.7;
	}

	/* Content */
	.hooks-content {
		flex: 1;
		overflow-y: auto;
		background: var(--background);
	}

	.hooks-loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px;
		color: var(--muted-foreground);
		gap: 12px;
	}

	.hooks-loading :global(svg) {
		color: var(--primary);
	}

	.hooks-list {
		padding: 16px;
	}

	.hook-items {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px 24px;
		text-align: center;
		color: var(--muted-foreground);
	}

	.empty-icon {
		width: 48px;
		height: 48px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--muted);
		border-radius: 50%;
		margin-bottom: 12px;
	}

	.empty-hint {
		font-size: 0.75rem;
		margin-top: 4px;
	}

	.add-first-hook-btn {
		margin-top: 12px;
		color: var(--primary);
		background: transparent;
		border: none;
		cursor: pointer;
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.add-first-hook-btn:hover {
		text-decoration: underline;
	}

	/* Footer */
	.hooks-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 12px 16px;
		border-top: 1px solid var(--border);
		background: var(--secondary);
		flex-shrink: 0;
	}

	.footer-info {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.footer-stats {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Form overlay */
	.form-overlay {
		position: absolute;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 16px;
		background: color-mix(in oklch, var(--background) 80%, transparent);
		backdrop-filter: blur(8px);
		-webkit-backdrop-filter: blur(8px);
	}

	/* Spinner animation */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* Mobile adjustments */
	.hooks-card.mobile .hooks-tabs {
		padding: 8px 12px;
	}

	.hooks-card.mobile .hooks-search-bar {
		flex-wrap: wrap;
		padding: 12px;
	}

	.hooks-card.mobile .search-input-wrapper {
		width: 100%;
	}

	.hooks-card.mobile .add-hook-btn {
		width: 100%;
		justify-content: center;
	}
</style>
