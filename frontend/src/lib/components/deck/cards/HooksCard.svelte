<script lang="ts">
	/**
	 * HooksCard - Hook management card for The Deck
	 *
	 * Features:
	 * - View and manage app-level hooks (no project)
	 * - View and manage project-scoped hooks
	 * - View plugin-contributed hooks (read-only)
	 * - Create, edit, delete, and test hooks
	 * - Enable/disable hooks
	 * - Page-based navigation with breadcrumbs (no modals)
	 */
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
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
	import { activeSession } from '$lib/stores/active-session';
	import { derived } from 'svelte/store';

	const selectedProject = derived(activeSession, $s => $s.project);
	import type { Hook, HookCreate, HookUpdate, HookTestResult } from '$lib/api/client';
	import HookListItem from '$lib/components/hooks/HookListItem.svelte';
	import HookForm from '$lib/components/hooks/HookForm.svelte';
	import PluginHookItem from '$lib/components/hooks/PluginHookItem.svelte';
	import { Search, X, Loader2, Zap, Plus, AlertCircle, CheckCircle, XCircle } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// ── View state ─────────────────────────────────────────────────────
	type View = 'list' | 'create' | 'edit';
	let currentView = $state<View>('list');

	type Tab = 'app' | 'project' | 'plugins';
	let activeTab = $state<Tab>('app');
	let searchQuery = $state('');

	// Selected hook for editing
	let selectedHook = $state<Hook | null>(null);
	let formSaving = $state(false);

	// Form ref for calling save() from footer
	let formRef: HookForm | undefined = $state(undefined);

	// Test result display
	let testResult = $state<{ hookId: string; result: HookTestResult } | null>(null);

	// ── Data loading ───────────────────────────────────────────────────
	$effect(() => {
		if ($selectedProject) {
			hooks.loadAll($selectedProject);
		} else {
			// Still load app hooks even without a project
			hooks.loadAppHooks();
		}
	});

	// ── Derived: filtered lists ────────────────────────────────────────

	// App hooks (project_id is null)
	const appHooks = $derived(() => {
		let items = $projectHooks.filter((h: Hook) => !h.project_id);
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			items = items.filter(
				(h: Hook) =>
					h.name.toLowerCase().includes(query) ||
					h.event.toLowerCase().includes(query) ||
					h.command?.toLowerCase().includes(query) ||
					h.url?.toLowerCase().includes(query)
			);
		}
		return items;
	});

	// Project-scoped hooks
	const filteredProjectHooks = $derived(() => {
		let items = $projectHooks.filter((h: Hook) => !!h.project_id);
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			items = items.filter(
				(h: Hook) =>
					h.name.toLowerCase().includes(query) ||
					h.event.toLowerCase().includes(query) ||
					h.command?.toLowerCase().includes(query) ||
					h.url?.toLowerCase().includes(query)
			);
		}
		return items;
	});

	// Plugin hooks
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

	// ── Counts for tabs ────────────────────────────────────────────────
	const appHookCount = $derived($projectHooks.filter((h: Hook) => !h.project_id).length);
	const projectHookCount = $derived($projectHooks.filter((h: Hook) => !!h.project_id).length);

	// ── Navigation ─────────────────────────────────────────────────────
	function backToList() {
		currentView = 'list';
		selectedHook = null;
		formRef = undefined;
	}

	function openCreate() {
		selectedHook = null;
		formRef = undefined;
		currentView = 'create';
	}

	function openEdit(hook: Hook) {
		selectedHook = hook;
		formRef = undefined;
		currentView = 'edit';
	}

	// ── CRUD handlers ──────────────────────────────────────────────────
	async function handleSave() {
		formRef?.save();
	}

	async function handleFormSave(data: HookCreate | HookUpdate, projectId: string | null = null) {
		formSaving = true;
		try {
			if (selectedHook) {
				await hooks.updateHook(selectedHook.id, data as HookUpdate);
			} else if (projectId) {
				// Project-scoped hook
				await hooks.createHook(projectId, data as HookCreate);
			} else {
				// App-level hook — no project_id
				await hooks.createAppHook(data as HookCreate);
			}
			backToList();
		} catch (e) {
			console.error('Failed to save hook:', e);
		} finally {
			formSaving = false;
		}
	}

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
		{ id: 'app' as Tab, label: 'App', count: appHookCount },
		{ id: 'project' as Tab, label: 'Project', count: projectHookCount },
		{ id: 'plugins' as Tab, label: 'Plugin', count: $hookStats.totalPlugin }
	]);
</script>

<!-- ═══════════════════ CREATE / EDIT VIEW ═══════════════════ -->
{#if currentView === 'create' || currentView === 'edit'}
	<FeaturePageLayout
		icon={Zap}
		title="Hooks"
		breadcrumbs={[
			{ label: 'Hooks', onclick: backToList },
			{ label: currentView === 'edit' && selectedHook ? selectedHook.name : 'New Hook' }
		]}
		noPadding={true}
	>
		{#snippet footer()}
			<button
				class="fp-btn fp-btn--primary"
				onclick={handleSave}
				disabled={formSaving}
			>
				{#if formSaving}
					<Loader2 size={14} class="animate-spin" />
					<span class="btn-label">Saving...</span>
				{:else}
					<span class="btn-label">{currentView === 'edit' ? 'Save Changes' : 'Create Hook'}</span>
				{/if}
			</button>
		{/snippet}
		{#snippet children()}
			<div class="hooks-card card-system">
				<HookForm
					bind:this={formRef}
					hook={selectedHook}
					onSave={handleFormSave}
				/>
			</div>
		{/snippet}
	</FeaturePageLayout>

<!-- ═══════════════════ LIST VIEW ═══════════════════ -->
{:else}
	<FeaturePageLayout
		icon={Zap}
		title="Hooks"
		itemCount={appHookCount + projectHookCount}
		itemLabel="hook"
	>
		{#snippet headerTabs()}
			<div class="fp-tab-bar">
				{#each tabs as tab}
					<button
						class="fp-tab"
						class:active={activeTab === tab.id}
						onclick={() => activeTab = tab.id}
					>
						{tab.label}
						<span class="fp-tab-count">{tab.count}</span>
					</button>
				{/each}
			</div>
		{/snippet}
		{#snippet footer()}
			{#if activeTab !== 'plugins'}
				<button class="fp-btn fp-btn--primary" onclick={openCreate} title="New Hook">
					<Plus size={14} /> <span class="btn-label">New Hook</span>
				</button>
			{/if}
		{/snippet}
		{#snippet children()}
			<div class="hooks-card card-system" style="position: relative;">
				<!-- Search -->
				<div class="fp-search-bar">
					<div class="fph-search-wrapper">
						<Search size={14} />
						<input
							type="text"
							bind:value={searchQuery}
							placeholder="Search hooks..."
							class="fph-search-input"
						/>
					</div>
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

					{:else if activeTab === 'app'}
						<div class="hooks-list">
							{#if appHooks().length === 0}
								<div class="empty-state">
									<div class="empty-icon"><Zap size={24} /></div>
									<p>No app-level hooks</p>
									<p class="empty-hint">App hooks fire for all sessions, regardless of project</p>
									<button onclick={openCreate} class="add-first-hook-btn">
										Add your first app hook
									</button>
								</div>
							{:else}
								<div class="hook-items">
									{#each appHooks() as hook (hook.id)}
										<HookListItem
											{hook}
											testing={$hookTesting[hook.id] || false}
											saving={$hookSaving[hook.id] || false}
											onToggle={() => hooks.toggleHook(hook.id)}
											onEdit={() => openEdit(hook)}
											onDelete={() => handleDeleteHook(hook.id)}
											onTest={() => handleTestHook(hook.id)}
										/>
									{/each}
								</div>
							{/if}
						</div>

					{:else if activeTab === 'project'}
						<div class="hooks-list">
							{#if !$selectedProject}
								<div class="empty-state">
									<div class="empty-icon"><Zap size={24} /></div>
									<p>No project selected</p>
									<p class="empty-hint">Select a project to manage its hooks</p>
								</div>
							{:else if filteredProjectHooks().length === 0}
								<div class="empty-state">
									<div class="empty-icon"><Zap size={24} /></div>
									<p>No project hooks</p>
									<button onclick={openCreate} class="add-first-hook-btn">
										Add your first project hook
									</button>
								</div>
							{:else}
								<div class="hook-items">
									{#each filteredProjectHooks() as hook (hook.id)}
										<HookListItem
											{hook}
											testing={$hookTesting[hook.id] || false}
											saving={$hookSaving[hook.id] || false}
											onToggle={() => hooks.toggleHook(hook.id)}
											onEdit={() => openEdit(hook)}
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
									<div class="empty-icon"><Zap size={24} /></div>
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
			</div>
		{/snippet}
	</FeaturePageLayout>
{/if}

<style>
	.hooks-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	/* Test result toast */
	.test-result {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 16px;
		padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
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
		from { opacity: 0; transform: translateY(-8px); }
		to { opacity: 1; transform: translateY(0); }
	}

	/* Error display */
	.hooks-error {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 12px 16px 0;
		padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
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
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		transition: opacity 150ms ease;
	}

	.error-close:hover {
		opacity: 0.7;
	}

	/* Content */
	.hooks-content {
		flex: 1;
		overflow-y: auto;
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

	/* Search bar */
	.fp-search-bar {
		max-width: 640px;
		width: 100%;
		margin: 0 auto;
	}

	.fp-search-bar :global(.fph-search-wrapper) {
		width: 100%;
	}

	.fp-search-bar :global(.fph-search-input) {
		width: 100% !important;
	}

	.btn-label {
		display: inline;
	}

	/* Spinner animation */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
