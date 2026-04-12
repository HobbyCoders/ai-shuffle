<script lang="ts">
	import { Bot, Search, Plus, Trash2, Download, Upload, X, RotateCcw, Sparkles, ChevronRight } from 'lucide-svelte';
	import { groups } from '$lib/stores/groups';
	import type { SubagentExport } from '$lib/api/client';
	import AIGenerateDialog from '../AIGenerateDialog.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import '../card-design-system.css';

	interface Subagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
		is_builtin: boolean;
		is_modified: boolean;
		created_at: string;
		updated_at: string;
	}

	interface Props {
		subagents: Subagent[];
		loading: boolean;
		error: string | null;
		searchQuery: string;
		onSearchQueryChange: (value: string) => void;
		deletingId: string | null;
		deleteLoading: boolean;
		revertingId: string | null;
		revertLoading: boolean;
		importing: boolean;
		showGenerateDialog: boolean;
		onShowGenerateDialogChange: (value: boolean) => void;
		onEdit: (agent: Subagent) => void;
		onCreate: () => void;
		onDeleteClick: (id: string, e: Event) => void;
		onConfirmDelete: () => void;
		onCancelDelete: () => void;
		onRevert: (id: string, e: Event) => void;
		onConfirmRevert: () => void;
		onCancelRevert: () => void;
		onExport: (agentId: string, e: Event) => void;
		onTriggerImport: () => void;
		onClearError: () => void;
		onGeneratedSubagent: (data: SubagentExport | unknown) => void;
		getModelDisplay: (model?: string) => string;
		getModelBadgeClass: (model?: string) => string;
		filteredSubagents: () => Subagent[];
	}

	let {
		subagents,
		loading,
		error,
		searchQuery,
		onSearchQueryChange,
		deletingId,
		deleteLoading,
		revertingId,
		revertLoading,
		importing,
		showGenerateDialog,
		onShowGenerateDialogChange,
		onEdit,
		onCreate,
		onDeleteClick,
		onConfirmDelete,
		onCancelDelete,
		onRevert,
		onConfirmRevert,
		onCancelRevert,
		onExport,
		onTriggerImport,
		onClearError,
		onGeneratedSubagent,
		getModelDisplay,
		getModelBadgeClass,
		filteredSubagents,
	}: Props = $props();
</script>

<FeaturePageLayout
	icon={Bot}
	title="Subagents"
	itemCount={subagents.length}
	itemLabel="subagents"
>
	{#snippet footer()}
		<div class="pf-footer">
			<div class="pf-footer-left">
				<button
					onclick={() => onShowGenerateDialogChange(true)}
					class="fp-btn fp-btn--secondary"
					title="Generate with AI"
				>
					<Sparkles size={14} /> <span class="btn-label">AI Generate</span>
				</button>
				<button
					onclick={onTriggerImport}
					disabled={importing}
					class="fp-btn fp-btn--secondary"
					title="Import from file"
				>
					{#if importing}
						<div class="card-spinner card-spinner--small"></div>
					{:else}
						<Upload size={14} /> <span class="btn-label">Import</span>
					{/if}
				</button>
			</div>
			<div class="pf-footer-right">
				<button
					onclick={onCreate}
					class="fp-btn fp-btn--primary"
				>
					<Plus size={14} /> <span class="btn-label">New Subagent</span>
				</button>
			</div>
		</div>
	{/snippet}

	{#if subagents.length > 3}
		<div class="fp-search-bar">
			<div class="fph-search-wrapper">
				<Search />
				<input
					type="text"
					value={searchQuery}
					oninput={(e) => onSearchQueryChange(e.currentTarget.value)}
					placeholder="Search subagents..."
					class="fph-search-input"
				/>
			</div>
		</div>
	{/if}

	{#if error}
		<div class="card-error-banner">
			<span class="flex-1">{error}</span>
			<button onclick={onClearError}>
				<X class="w-4 h-4" />
			</button>
		</div>
	{/if}

	{#if loading}
		<div class="loading-container">
			<div class="card-spinner"></div>
		</div>
	{:else if filteredSubagents().length === 0}
		<div class="card-empty-state">
			{#if searchQuery}
				<Search class="card-empty-icon" />
				<p class="card-empty-title">No subagents match "{searchQuery}"</p>
				<button onclick={() => onSearchQueryChange('')} class="card-empty-action">
					Clear search
				</button>
			{:else}
				<Bot class="card-empty-icon" />
				<p class="card-empty-title">No subagents configured yet</p>
				<p class="card-empty-description">
					Subagents are specialized AI assistants that can be delegated to for specific tasks.
				</p>
			{/if}
		</div>
	{:else}
		<div class="fp-list">
			{#each filteredSubagents() as agent, index (agent.id)}
				{#if index > 0}<div class="fp-list-divider"></div>{/if}
				{#if deletingId === agent.id}
					<div class="fp-list-item" style="border-color: color-mix(in oklch, var(--destructive) 40%, transparent);">
						<div class="fp-item-content">
							<p class="fp-item-name">
								Delete <strong>{agent.name}</strong>? This cannot be undone.
							</p>
							<div style="display: flex; gap: var(--space-2); margin-top: var(--space-2);">
								<button
									onclick={onCancelDelete}
									disabled={deleteLoading}
									class="fp-btn fp-btn--secondary"
								>
									Cancel
								</button>
								<button
									onclick={onConfirmDelete}
									disabled={deleteLoading}
									class="fp-btn fp-btn--primary"
									style="background: var(--destructive);"
								>
									{#if deleteLoading}
										<div class="card-spinner card-spinner--small"></div>
									{/if}
									Delete
								</button>
							</div>
						</div>
					</div>
				{:else if revertingId === agent.id}
					<div class="fp-list-item" style="border-color: color-mix(in oklch, var(--warning) 40%, transparent);">
						<div class="fp-item-content">
							<p class="fp-item-name">
								Revert <strong>{agent.name}</strong> to original defaults? Your customizations will be lost.
							</p>
							<div style="display: flex; gap: var(--space-2); margin-top: var(--space-2);">
								<button
									onclick={onCancelRevert}
									disabled={revertLoading}
									class="fp-btn fp-btn--secondary"
								>
									Cancel
								</button>
								<button
									onclick={onConfirmRevert}
									disabled={revertLoading}
									class="fp-btn fp-btn--primary"
								>
									{#if revertLoading}
										<div class="card-spinner card-spinner--small"></div>
									{/if}
									Revert
								</button>
							</div>
						</div>
					</div>
				{:else}
					<div
						class="fp-list-item"
						onclick={() => onEdit(agent)}
						role="button"
						tabindex="0"
						onkeydown={(e) => e.key === 'Enter' && onEdit(agent)}
					>
						<div class="fp-item-indicator"></div>
						<div class="fp-item-content">
							<div class="fp-item-header">
								<span class="fp-item-name">{agent.name}</span>
								{#if agent.is_builtin}
									<span class="fp-item-badge">Built-in</span>
									{#if agent.is_modified}
										<span class="fp-item-badge" style="color: var(--warning);">Modified</span>
									{/if}
								{/if}
								{#if $groups.subagents.assignments[agent.id]}
									<span class="fp-item-badge fp-item-badge--accent">{$groups.subagents.assignments[agent.id]}</span>
								{/if}
							</div>
							<div class="fp-item-meta">
								<span class="fp-meta-model">{getModelDisplay(agent.model)}</span>
								{#if agent.tools && agent.tools.length > 0}
									<span class="fp-meta-sep">·</span>
									<span class="fp-meta-chip">{agent.tools.length} tools</span>
								{/if}
							</div>
						</div>
						<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
							{#if agent.is_builtin && agent.is_modified}
								<button
									onclick={(e) => onRevert(agent.id, e)}
									class="card-action-btn"
									title="Revert to defaults"
								>
									<RotateCcw size={16} />
								</button>
							{/if}
							<button
								class="card-action-btn"
								title="Export"
								onclick={(e) => onExport(agent.id, e)}
							>
								<Download size={16} />
							</button>
							{#if !agent.is_builtin}
								<button
									class="card-action-btn card-action-btn--destructive"
									title="Delete"
									onclick={(e) => { e.stopPropagation(); onDeleteClick(agent.id, e); }}
								>
									<Trash2 size={16} />
								</button>
							{/if}
						</div>
						<div class="fp-item-chevron"><ChevronRight size={16} /></div>
					</div>
				{/if}
			{/each}
		</div>
	{/if}

	<!-- AI Generate Dialog -->
	<AIGenerateDialog
		type="subagent"
		isOpen={showGenerateDialog}
		onClose={() => onShowGenerateDialogChange(false)}
		onGenerated={onGeneratedSubagent}
	/>
</FeaturePageLayout>

<style>
	.flex-1 {
		flex: 1;
	}

	/* Loading state */
	.loading-container {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-8);
	}

	/* Search bar between header and list */
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

	/* ─── Footer (matches ProfileCard) ─── */
	.pf-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
	}

	.pf-footer-left {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.pf-footer-right {
		display: flex;
		align-items: center;
		gap: 10px;
	}
</style>
