<script lang="ts">
	/**
	 * ProjectAgentsTab - Agent list tab for project detail view
	 *
	 * Features:
	 * - View agents in .claude/agents/
	 * - Delete agents
	 * - Search/filter
	 */
	import {
		projectAgents,
		allProjectAgents,
		projectAgentsLoading,
		projectAgentsError,
		projectAgentCount
	} from '$lib/stores/projectAgents';
	import { Search, Bot, Trash2, X, ChevronRight } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Search state
	let searchQuery = $state('');

	// Delete confirmation
	let deletingAgent = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Load agents when project changes
	$effect(() => {
		if (projectId) {
			projectAgents.loadAgents(projectId);
		}
	});

	// Filter agents
	const filteredAgents = $derived(() => {
		if (!searchQuery.trim()) return $allProjectAgents;
		const q = searchQuery.toLowerCase();
		return $allProjectAgents.filter(
			(a) =>
				a.name.toLowerCase().includes(q) ||
				a.description.toLowerCase().includes(q)
		);
	});

	// Model display helpers
	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit' },
		{ value: 'haiku', label: 'Haiku' },
		{ value: 'sonnet', label: 'Sonnet' },
		{ value: 'opus', label: 'Opus' },
		{ value: 'opus[1m]', label: 'Opus 1M' }
	];

	function getModelBadgeClass(model?: string | null): string {
		if (!model || model === '') return 'card-badge--inherit';
		if (model.startsWith('opus')) return 'card-badge--opus';
		return `card-badge--${model}`;
	}

	function getModelDisplay(model?: string | null): string {
		const opt = MODEL_OPTIONS.find(o => o.value === model);
		return opt?.label || 'Inherit';
	}

	// Delete agent
	async function handleDelete(agentName: string) {
		deleteLoading = true;
		try {
			await projectAgents.deleteAgent(projectId, agentName);
			deletingAgent = null;
		} catch (e) {
			console.error('Failed to delete agent:', e);
		} finally {
			deleteLoading = false;
		}
	}
</script>

<div class="card-system agents-tab">
	{#if $projectAgentsLoading}
		<div class="card-content">
			<div class="loading-container">
				<div class="card-spinner"></div>
			</div>
		</div>
	{:else}
		<!-- Search bar (show if > 3 agents) -->
		{#if $projectAgentCount > 3}
			<div class="card-search-wrapper">
				<div class="card-search-container">
					<Search class="card-search-icon" />
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search agents..."
						class="card-search-input"
					/>
				</div>
			</div>
		{/if}

		<!-- Content -->
		<div class="card-content">
			<div class="centered-content">
				{#if $projectAgentsError}
					<div class="card-error-banner">
						<span class="flex-1">{$projectAgentsError}</span>
						<button onclick={() => projectAgents.clearError()}>
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				{#if filteredAgents().length === 0}
					<div class="card-empty-state">
						{#if searchQuery}
							<Search class="card-empty-icon" />
							<p class="card-empty-title">No agents match "{searchQuery}"</p>
							<button onclick={() => (searchQuery = '')} class="card-empty-action">
								Clear search
							</button>
						{:else}
							<Bot class="card-empty-icon" />
							<p class="card-empty-title">No agents defined</p>
							<p class="card-empty-description">
								Create agents from the Subagents card to add specialized capabilities.
							</p>
						{/if}
					</div>
				{:else}
					{#each filteredAgents() as agent, index (agent.name)}
						{#if index > 0}<div class="fp-list-divider"></div>{/if}
						{#if deletingAgent === agent.name}
							<!-- Inline delete confirmation -->
							<div class="fp-list-item fp-list-item--deleting">
								<div class="fp-item-content">
									<p class="delete-confirm-text">
										Delete <strong>{agent.name}</strong>? This cannot be undone.
									</p>
									<div class="delete-confirm-actions">
										<button
											onclick={() => (deletingAgent = null)}
											disabled={deleteLoading}
											class="card-btn-secondary"
										>
											Cancel
										</button>
										<button
											onclick={() => handleDelete(agent.name)}
											disabled={deleteLoading}
											class="card-btn-destructive"
										>
											{#if deleteLoading}
												<div class="card-spinner card-spinner--small"></div>
											{/if}
											Delete
										</button>
									</div>
								</div>
							</div>
						{:else}
							<div class="fp-list-item">
								<div class="fp-item-indicator"></div>
								<div class="fp-item-content">
									<div class="fp-item-header">
										<span class="fp-item-name">{agent.name}</span>
										<span class="fp-item-badge">
											{getModelDisplay(agent.model)}
										</span>
									</div>
									<div class="fp-item-meta">
										<span class="fp-meta-model">{getModelDisplay(agent.model)}</span>
										{#if agent.tools && agent.tools.length > 0}
											<span class="fp-meta-sep">&middot;</span>
											<span class="fp-meta-chip">{agent.tools.length} tools</span>
										{/if}
									</div>
								</div>
								<div class="fp-item-actions">
									<button
										onclick={(e) => { e.stopPropagation(); deletingAgent = agent.name; }}
										class="card-action-btn card-action-btn--destructive"
										title="Delete"
									>
										<Trash2 />
									</button>
								</div>
								<div class="fp-item-chevron">
									<ChevronRight size={16} />
								</div>
							</div>
						{/if}
					{/each}
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.agents-tab {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.centered-content {
		width: 100%;
		display: flex;
		flex-direction: column;
		gap: var(--space-4, 16px);
	}

	.flex-1 {
		flex: 1;
	}

	.card-search-wrapper {
		padding: 0 var(--space-4) var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
		background: linear-gradient(180deg, transparent 0%, color-mix(in oklch, var(--muted) 20%, transparent) 100%);
	}

	.loading-container {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-8);
	}

	/* Center actions on mobile */
	@media (max-width: 639px) {
		.agents-tab :global(.fp-item-actions) {
			justify-content: center;
		}
	}
</style>
