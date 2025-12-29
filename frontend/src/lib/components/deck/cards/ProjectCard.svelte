<script lang="ts">
	/**
	 * ProjectCard - Project management card for The Deck
	 *
	 * Features:
	 * - Project list with name, path display
	 * - Group badge display
	 * - Delete with inline confirmation
	 * - Create form that toggles in place
	 * - Search/filter for many projects
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { tabs, projects } from '$lib/stores/tabs';
	import { groups } from '$lib/stores/groups';
	import { Search, Plus, Trash2, FolderOpen, X, Download } from 'lucide-svelte';
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

	// Form state
	let showCreateForm = $state(false);
	let newProjectId = $state('');
	let newProjectName = $state('');
	let newProjectDescription = $state('');
	let creating = $state(false);
	let error = $state<string | null>(null);

	// Search state
	let searchQuery = $state('');

	// Delete confirmation state
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Filtered projects
	const filteredProjects = $derived(() => {
		if (!searchQuery.trim()) return $projects;
		const q = searchQuery.toLowerCase();
		return $projects.filter(
			p =>
				p.name.toLowerCase().includes(q) ||
				p.id.toLowerCase().includes(q) ||
				p.path.toLowerCase().includes(q)
		);
	});

	// Create project handler
	async function handleCreateProject() {
		if (!newProjectId.trim() || !newProjectName.trim()) return;

		creating = true;
		error = null;

		try {
			await tabs.createProject({
				id: newProjectId.trim(),
				name: newProjectName.trim(),
				description: newProjectDescription.trim() || undefined
			});
			// Reset form
			newProjectId = '';
			newProjectName = '';
			newProjectDescription = '';
			showCreateForm = false;
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to create project';
		} finally {
			creating = false;
		}
	}

	// Delete project handler
	async function handleDeleteProject() {
		if (!deletingId) return;

		deleteLoading = true;
		error = null;

		try {
			await tabs.deleteProject(deletingId);
			deletingId = null;
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete project';
		} finally {
			deleteLoading = false;
		}
	}

	// Cancel create form
	function cancelCreate() {
		showCreateForm = false;
		newProjectId = '';
		newProjectName = '';
		newProjectDescription = '';
		error = null;
	}
</script>

{#snippet content()}
	<div class="card-system project-card" class:maximized={card.maximized}>
		<!-- Header -->
		<div class="card-header">
			<div class="card-header-info">
				{#if mobile}
					<span class="card-header-title">Projects</span>
				{/if}
				<span class="card-header-subtitle">
					{$projects.length} project{$projects.length !== 1 ? 's' : ''} configured
				</span>
			</div>
		</div>

		<!-- Search (show if > 3 projects) -->
		{#if $projects.length > 3}
			<div class="card-search-wrapper">
				<div class="card-search-container">
					<Search class="card-search-icon" />
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search projects..."
						class="card-search-input"
					/>
				</div>
			</div>
		{/if}

		<!-- Content -->
		<div class="card-content">
			<div class="centered-content">
			{#if error}
				<div class="card-error-banner">
					<span class="flex-1">{error}</span>
					<button onclick={() => (error = null)}>
						<X class="w-4 h-4" />
					</button>
				</div>
			{/if}

			<!-- Delete confirmation banner -->
			{#if deletingId}
				<div class="card-delete-banner">
					<p>
						Delete <strong>{$projects.find(p => p.id === deletingId)?.name}</strong>? This cannot be undone.
					</p>
					<div class="button-group">
						<button
							onclick={() => (deletingId = null)}
							disabled={deleteLoading}
							class="card-btn-secondary flex-1"
						>
							Cancel
						</button>
						<button
							onclick={handleDeleteProject}
							disabled={deleteLoading}
							class="card-btn-destructive flex-1"
						>
							{#if deleteLoading}
								<div class="card-spinner card-spinner--small"></div>
							{/if}
							Delete
						</button>
					</div>
				</div>
			{/if}

			<!-- Project list -->
			{#if filteredProjects().length === 0}
				<div class="card-empty-state">
					{#if searchQuery}
						<Search class="card-empty-icon" />
						<p class="card-empty-title">No projects match "{searchQuery}"</p>
						<button onclick={() => (searchQuery = '')} class="card-empty-action">
							Clear search
						</button>
					{:else}
						<FolderOpen class="card-empty-icon" />
						<p class="card-empty-title">No projects configured yet</p>
						<p class="card-empty-description">
							Projects organize your work into separate workspaces with their own settings.
						</p>
					{/if}
				</div>
			{:else}
				{#each filteredProjects() as project (project.id)}
					<div class="card-list-item">
						<div class="card-item-icon">
							<FolderOpen />
						</div>
						<div class="card-item-content">
							<div class="card-item-header">
								<span class="card-item-name">{project.name}</span>
								{#if $groups.projects.assignments[project.id]}
									<span class="card-badge card-badge--primary">
										{$groups.projects.assignments[project.id]}
									</span>
								{/if}
							</div>
							<span class="card-item-id">/workspace/{project.path}/</span>
						</div>
						<div class="card-item-actions">
							<button
								onclick={() => (deletingId = project.id)}
								class="card-action-btn card-action-btn--destructive"
								title="Delete project"
							>
								<Trash2 />
							</button>
						</div>
					</div>
				{/each}
			{/if}
			</div>
		</div>

		<!-- Footer -->
		<div class="card-footer">
			{#if showCreateForm}
				<div class="create-form">
					<div class="form-row">
						<div class="form-field">
							<label class="card-form-label">ID</label>
							<input
								bind:value={newProjectId}
								class="card-form-input card-form-input--mono"
								placeholder="my-project"
							/>
						</div>
						<div class="form-field">
							<label class="card-form-label">Name</label>
							<input
								bind:value={newProjectName}
								class="card-form-input"
								placeholder="My Project"
							/>
						</div>
					</div>
					<div class="form-field">
						<label class="card-form-label">Description</label>
						<textarea
							bind:value={newProjectDescription}
							class="card-form-input card-form-textarea"
							rows="2"
							placeholder="Optional description..."
						></textarea>
					</div>
					<div class="form-actions">
						<button
							onclick={cancelCreate}
							disabled={creating}
							class="card-btn-secondary"
						>
							Cancel
						</button>
						<button
							onclick={handleCreateProject}
							disabled={creating || !newProjectId.trim() || !newProjectName.trim()}
							class="card-btn-primary"
						>
							{#if creating}
								<div class="card-spinner card-spinner--small"></div>
							{/if}
							Create
						</button>
					</div>
				</div>
			{:else}
				<button
					onclick={() => (showCreateForm = true)}
					class="card-btn-primary"
				>
					<Plus />
					New Project
				</button>
			{/if}
		</div>
	</div>
{/snippet}

{#if mobile}
	<!-- Mobile: No BaseCard wrapper, just the content -->
	{@render content()}
{:else}
	<!-- Desktop: Wrap in BaseCard -->
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
		{@render content()}
	</BaseCard>
{/if}

<style>
	.project-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--card);
		overflow: hidden;
	}

	/* Maximized state - constrain width and center content */
	.project-card.maximized {
		--card-max-width: 800px;
	}

	.project-card.maximized :global(.card-header),
	.project-card.maximized .card-search-wrapper,
	.project-card.maximized :global(.card-footer) {
		max-width: var(--card-max-width);
		margin-left: auto;
		margin-right: auto;
		width: 100%;
	}

	/* Centered content wrapper - prevents content from stretching too wide on large screens */
	.centered-content {
		width: 100%;
		max-width: 600px;
		margin: 0 auto;
	}

	.project-card.maximized .centered-content {
		max-width: var(--card-max-width);
	}

	.card-search-wrapper {
		padding: 0 var(--space-4) var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
		background: linear-gradient(180deg, transparent 0%, color-mix(in oklch, var(--muted) 20%, transparent) 100%);
	}

	/* Create form styles */
	.create-form {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		width: 100%;
	}

	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-3);
	}

	.form-field {
		display: flex;
		flex-direction: column;
	}

	.form-actions {
		display: flex;
		gap: var(--space-2);
		margin-top: var(--space-2);
	}

	.form-actions .card-btn-secondary,
	.form-actions .card-btn-primary {
		flex: 1;
	}

	/* Ensure footer adapts when form is showing */
	.project-card :global(.card-footer) {
		flex-direction: column;
	}

	/* Text utilities */
	.flex-1 {
		flex: 1;
	}

	/* Responsive adjustments */
	@media (max-width: 400px) {
		.form-row {
			grid-template-columns: 1fr;
		}
	}
</style>
