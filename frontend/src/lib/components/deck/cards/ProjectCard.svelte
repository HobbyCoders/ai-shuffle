<script lang="ts">
	/**
	 * ProjectCard - Project management card for The Deck
	 *
	 * Features:
	 * - Project list with name, path display
	 * - Click project to view detail with tabs:
	 *   - Plugins (LOCAL and PROJECT scope)
	 *   - Agents (file-based in .claude/agents/)
	 *   - Hooks (project-scoped custom hooks)
	 * - Group badge display
	 * - Delete with inline confirmation
	 * - Create form that toggles in place
	 * - Search/filter for many projects
	 *
	 * Modes:
	 * - embedded=false (default): Uses BaseCard wrapper for deck, direct render for mobile
	 * - embedded=true: Uses FeaturePageLayout for panel integration
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { tabs, projects } from '$lib/stores/tabs';
	import { groups } from '$lib/stores/groups';
	import { Search, Plus, Trash2, FolderOpen, X, ChevronLeft, Package, Bot, Zap, MessageSquare } from 'lucide-svelte';
	import GroupDropdown from '$lib/components/GroupDropdown.svelte';
	import ProjectPluginsTab from './ProjectPluginsTab.svelte';
	import ProjectAgentsTab from './ProjectAgentsTab.svelte';
	import ProjectHooksTab from './ProjectHooksTab.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
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
		embedded?: boolean;
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
		mobile = false,
		embedded = false
	}: Props = $props();

	// View mode: list or detail
	type ViewMode = 'list' | 'detail';
	let viewMode = $state<ViewMode>('list');
	let selectedProjectId = $state<string | null>(null);

	// Detail view tab
	type DetailTab = 'plugins' | 'agents' | 'hooks';
	let activeTab = $state<DetailTab>('plugins');

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

	// Get selected project
	const selectedProject = $derived($projects.find(p => p.id === selectedProjectId));

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

	// Handle project click - open detail view
	function handleProjectClick(projectId: string) {
		selectedProjectId = projectId;
		viewMode = 'detail';
		activeTab = 'plugins';
	}

	// Back to list view
	function handleBack() {
		viewMode = 'list';
		selectedProjectId = null;
	}

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

{#snippet deleteConfirmation(project: typeof $projects[0])}
	<div class="card-list-item card-list-item--deleting fp-list-item fp-list-item--deleting">
		<div class="card-item-icon card-item-icon--destructive fp-item-icon fp-item-icon--destructive">
			<Trash2 />
		</div>
		<div class="card-item-content fp-item-content">
			{#if project.session_count && project.session_count > 0}
				<div class="delete-warning">
					<div class="delete-warning-header">
						<svg class="warning-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<path d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
						</svg>
						<span>This project has {project.session_count} conversation{project.session_count === 1 ? '' : 's'}</span>
					</div>
					<p class="delete-warning-text">
						Deleting <strong>{project.name}</strong> will permanently delete all associated conversations. This cannot be undone.
					</p>
				</div>
			{:else}
				<p class="delete-confirm-text">
					Delete <strong>{project.name}</strong>? This cannot be undone.
				</p>
			{/if}
			<div class="delete-confirm-actions">
				<button
					onclick={() => (deletingId = null)}
					disabled={deleteLoading}
					class="card-btn-secondary fp-btn fp-btn--secondary"
				>
					Cancel
				</button>
				<button
					onclick={handleDeleteProject}
					disabled={deleteLoading}
					class="card-btn-destructive fp-btn fp-btn--destructive"
				>
					{#if deleteLoading}
						<div class="card-spinner card-spinner--small fp-spinner"></div>
					{/if}
					{project.session_count && project.session_count > 0 ? 'Delete All' : 'Delete'}
				</button>
			</div>
		</div>
	</div>
{/snippet}

{#if embedded}
	<!-- ═══════════════════════════════════════════════════════════════════════════
	     EMBEDDED MODE - Uses FeaturePageLayout
	     ═══════════════════════════════════════════════════════════════════════════ -->
	{#if viewMode === 'detail' && selectedProject}
		<!-- Detail View -->
		<FeaturePageLayout
			icon={FolderOpen}
			title="Projects"
			breadcrumbs={[
				{ label: 'Projects', onclick: handleBack },
				{ label: selectedProject.name }
			]}
			subtitle={`/workspace/${selectedProject.path}/`}
		>
			{#snippet headerTabs()}
				<div class="fp-tab-bar">
					<button class="fp-tab" class:active={activeTab === 'plugins'} onclick={() => (activeTab = 'plugins')}>
						<Package size={14} /> Plugins
					</button>
					<button class="fp-tab" class:active={activeTab === 'agents'} onclick={() => (activeTab = 'agents')}>
						<Bot size={14} /> Agents
					</button>
					<button class="fp-tab" class:active={activeTab === 'hooks'} onclick={() => (activeTab = 'hooks')}>
						<Zap size={14} /> Hooks
					</button>
				</div>
			{/snippet}

			{#if activeTab === 'plugins'}
				<ProjectPluginsTab projectId={selectedProject.id} />
			{:else if activeTab === 'agents'}
				<ProjectAgentsTab projectId={selectedProject.id} />
			{:else if activeTab === 'hooks'}
				<ProjectHooksTab projectId={selectedProject.id} />
			{/if}
		</FeaturePageLayout>
	{:else}
		<!-- List View -->
		<FeaturePageLayout
			icon={FolderOpen}
			title="Projects"
			itemCount={$projects.length}
			itemLabel="projects"
		>
			{#snippet headerActions()}
				<!-- Search -->
				<div class="fph-search-wrapper">
					<Search size={14} />
					<input type="text" bind:value={searchQuery} placeholder="Search..." class="fph-search-input" />
				</div>
				<!-- Create button -->
				<button class="fp-btn fp-btn--primary" onclick={() => (showCreateForm = true)}>
					<Plus size={14} /> New Project
				</button>
			{/snippet}

			<!-- Error banner if any -->
			{#if error}
				<div class="fp-error-banner">
					<span class="flex-1">{error}</span>
					<button onclick={() => (error = null)}>
						<X size={14} />
					</button>
				</div>
			{/if}

			<!-- Create form (slide-down when showCreateForm=true) -->
			{#if showCreateForm}
				<div class="fp-create-form">
					<div class="fp-form-row">
						<div class="fp-form-field">
							<label class="fp-form-label">ID</label>
							<input class="fp-form-input fp-form-input--mono" bind:value={newProjectId} placeholder="my-project" />
						</div>
						<div class="fp-form-field">
							<label class="fp-form-label">Name</label>
							<input class="fp-form-input" bind:value={newProjectName} placeholder="My Project" />
						</div>
					</div>
					<div class="fp-form-field">
						<label class="fp-form-label">Description</label>
						<textarea class="fp-form-input fp-form-textarea" bind:value={newProjectDescription} rows="2" placeholder="Optional..."></textarea>
					</div>
					<div class="fp-form-actions">
						<button class="fp-btn fp-btn--secondary" onclick={cancelCreate}>Cancel</button>
						<button class="fp-btn fp-btn--primary" onclick={handleCreateProject} disabled={creating || !newProjectId.trim() || !newProjectName.trim()}>
							{#if creating}<div class="fp-spinner"></div>{/if}
							Create
						</button>
					</div>
				</div>
			{/if}

			<!-- Project List -->
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
				<div class="fp-list">
					{#each filteredProjects() as project (project.id)}
						{#if deletingId === project.id}
							{@render deleteConfirmation(project)}
						{:else}
							<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
							<div
								class="fp-list-item"
								onclick={() => handleProjectClick(project.id)}
								role="button"
								tabindex="0"
								onkeydown={(e) => e.key === 'Enter' && handleProjectClick(project.id)}
							>
								<div class="fp-item-icon fp-item-icon--primary">
									<FolderOpen size={18} />
								</div>
								<div class="fp-item-content">
									<div class="fp-item-header">
										<span class="fp-item-name">{project.name}</span>
										{#if $groups.projects.assignments[project.id]}
											<span class="card-badge card-badge--primary">
												{$groups.projects.assignments[project.id]}
											</span>
										{/if}
									</div>
									<span class="fp-item-id">/workspace/{project.path}/</span>
									<div class="fp-item-meta">
										<span class="fp-meta-chip"><MessageSquare size={12} /> {project.session_count || 0} sessions</span>
									</div>
								</div>
								<div class="fp-item-actions" onclick={(e) => e.stopPropagation()}>
									<GroupDropdown entityType="projects" entityId={project.id} />
									<button
										class="fp-action-btn fp-action-btn--destructive"
										onclick={(e) => { e.stopPropagation(); deletingId = project.id; }}
										title="Delete"
									>
										<Trash2 size={14} />
									</button>
								</div>
							</div>
						{/if}
					{/each}
				</div>
			{/if}
		</FeaturePageLayout>
	{/if}
{:else}
	<!-- ═══════════════════════════════════════════════════════════════════════════
	     NON-EMBEDDED MODE - Original BaseCard / mobile rendering
	     ═══════════════════════════════════════════════════════════════════════════ -->
	{#snippet content()}
		<div class="card-system project-card" class:maximized={card.maximized}>
			{#if viewMode === 'detail' && selectedProject}
				<!-- Detail View -->
				<div class="detail-view">
					<!-- Detail Header -->
					<div class="detail-header">
						<button onclick={handleBack} class="back-btn">
							<ChevronLeft class="w-5 h-5" />
						</button>
						<div class="detail-header-info">
							<span class="detail-header-title">{selectedProject.name}</span>
							<span class="detail-header-path">/workspace/{selectedProject.path}/</span>
						</div>
					</div>

					<!-- Detail Tabs -->
					<div class="detail-tabs">
						<button
							class="detail-tab"
							class:active={activeTab === 'plugins'}
							onclick={() => (activeTab = 'plugins')}
						>
							<Package class="w-4 h-4" />
							Plugins
						</button>
						<button
							class="detail-tab"
							class:active={activeTab === 'agents'}
							onclick={() => (activeTab = 'agents')}
						>
							<Bot class="w-4 h-4" />
							Agents
						</button>
						<button
							class="detail-tab"
							class:active={activeTab === 'hooks'}
							onclick={() => (activeTab = 'hooks')}
						>
							<Zap class="w-4 h-4" />
							Hooks
						</button>
					</div>

					<!-- Tab Content -->
					<div class="detail-content">
						{#if activeTab === 'plugins'}
							<ProjectPluginsTab projectId={selectedProject.id} />
						{:else if activeTab === 'agents'}
							<ProjectAgentsTab projectId={selectedProject.id} />
						{:else if activeTab === 'hooks'}
							<ProjectHooksTab projectId={selectedProject.id} />
						{/if}
					</div>
				</div>
			{:else}
				<!-- List View -->
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
							{#if deletingId === project.id}
								<!-- Inline delete confirmation -->
								<div class="card-list-item card-list-item--deleting">
									<div class="card-item-icon card-item-icon--destructive">
										<Trash2 />
									</div>
									<div class="card-item-content">
										{#if project.session_count && project.session_count > 0}
											<div class="delete-warning">
												<div class="delete-warning-header">
													<svg class="warning-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
														<path d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
													</svg>
													<span>This project has {project.session_count} conversation{project.session_count === 1 ? '' : 's'}</span>
												</div>
												<p class="delete-warning-text">
													Deleting <strong>{project.name}</strong> will permanently delete all associated conversations. This cannot be undone.
												</p>
											</div>
										{:else}
											<p class="delete-confirm-text">
												Delete <strong>{project.name}</strong>? This cannot be undone.
											</p>
										{/if}
										<div class="delete-confirm-actions">
											<button
												onclick={() => (deletingId = null)}
												disabled={deleteLoading}
												class="card-btn-secondary"
											>
												Cancel
											</button>
											<button
												onclick={handleDeleteProject}
												disabled={deleteLoading}
												class="card-btn-destructive"
											>
												{#if deleteLoading}
													<div class="card-spinner card-spinner--small"></div>
												{/if}
												{project.session_count && project.session_count > 0 ? 'Delete All' : 'Delete'}
											</button>
										</div>
									</div>
								</div>
							{:else}
								<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
								<div
									class="card-list-item card-list-item--clickable"
									onclick={() => handleProjectClick(project.id)}
									role="button"
									tabindex="0"
									onkeydown={(e) => e.key === 'Enter' && handleProjectClick(project.id)}
								>
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
									<div class="card-item-actions" onclick={(e) => e.stopPropagation()}>
										<!-- Group dropdown (portal-based to escape overflow:hidden) -->
										<GroupDropdown entityType="projects" entityId={project.id} />
										<button
											onclick={(e) => { e.stopPropagation(); deletingId = project.id; }}
											class="card-action-btn card-action-btn--destructive"
											title="Delete project"
										>
											<Trash2 />
										</button>
									</div>
								</div>
							{/if}
						{/each}
					{/if}
					</div>
				</div>

				<!-- Footer -->
				<div class="card-footer">
					<div class="card-footer-content">
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
			{/if}
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
{/if}

<style>
	/* ═══════════════════════════════════════════════════════════════════════════
	   NON-EMBEDDED (ORIGINAL) STYLES
	   ═══════════════════════════════════════════════════════════════════════════ */

	.project-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--card);
		overflow: hidden;
	}

	/* Centered content wrapper */
	.centered-content {
		width: 100%;
		display: flex;
		flex-direction: column;
		gap: var(--space-4, 16px);
	}

	.card-search-wrapper {
		padding: 0 var(--space-4) var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
		background: linear-gradient(180deg, transparent 0%, color-mix(in oklch, var(--muted) 20%, transparent) 100%);
	}

	/* Detail view styles */
	.detail-view {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.detail-header {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-bottom: 1px solid var(--border);
	}

	.back-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: var(--radius);
		background: var(--muted);
		border: 1px solid var(--border);
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.back-btn:hover {
		border-color: var(--accent);
		box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 25%, transparent);
		color: var(--foreground);
	}

	.back-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.detail-header-info {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.detail-header-title {
		font-weight: 600;
		font-size: var(--text-base);
	}

	.detail-header-path {
		font-size: var(--text-xs);
		color: var(--muted-foreground);
		font-family: var(--font-mono);
	}

	.detail-tabs {
		display: flex;
		gap: var(--space-1);
		padding: var(--space-2) var(--space-4);
		border-bottom: 1px solid var(--border);
		background: var(--muted);
	}

	.detail-tab {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
		border-radius: var(--radius);
		background: transparent;
		border: 1px solid transparent;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.detail-tab:hover {
		color: var(--foreground);
		border-color: var(--accent);
		box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 25%, transparent);
	}

	.detail-tab:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.detail-tab.active {
		background: var(--background);
		color: var(--foreground);
		font-weight: 500;
		border-color: var(--primary);
	}

	.detail-content {
		flex: 1;
		overflow: hidden;
		display: flex;
		flex-direction: column;
	}

	/* Clickable list item - inherits hover from card-design-system.css */
	.card-list-item--clickable {
		cursor: pointer;
		width: 100%;
		text-align: left;
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

	/* ═══════════════════════════════════════════════════════════════════════════
	   EMBEDDED MODE (fp-) STYLES — ProjectCard-specific overrides
	   ═══════════════════════════════════════════════════════════════════════════ */

	/* -----------------------------------------------------------------------
	   ERROR BANNER
	   ----------------------------------------------------------------------- */
	.fp-error-banner {
		display: flex;
		align-items: center;
		gap: var(--space-3, 12px);
		padding: var(--space-3, 12px) var(--space-4, 16px);
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		border-radius: var(--radius-md, 8px);
		color: var(--destructive);
		font-size: var(--text-sm, 0.75rem);
		box-shadow: var(--shadow-sm, 0 1px 3px rgba(0,0,0,0.12));
	}

	.fp-error-banner button {
		padding: 4px;
		background: transparent;
		border: none;
		color: inherit;
		cursor: pointer;
		border-radius: var(--radius-sm, 4px);
		transition: all 100ms cubic-bezier(0.4, 0, 0.2, 1);
	}

	.fp-error-banner button:hover {
		background: color-mix(in oklch, var(--destructive) 20%, transparent);
	}

	/* -----------------------------------------------------------------------
	   CREATE FORM
	   ----------------------------------------------------------------------- */
	.fp-create-form {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 12px);
		padding: var(--space-4, 16px);
		background: var(--surface-1, color-mix(in oklch, var(--muted) 60%, transparent));
		border: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
		border-radius: var(--radius-lg, 10px);
	}

	.fp-form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-3, 12px);
	}

	.fp-form-field {
		display: flex;
		flex-direction: column;
	}

	.fp-form-label {
		display: block;
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		color: var(--text-primary, var(--foreground));
		margin-bottom: var(--space-2, 8px);
	}

	.fp-form-input {
		width: 100%;
		padding: var(--space-2, 8px) var(--space-3, 12px);
		background: var(--surface-0, var(--card));
		border: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
		border-radius: var(--radius-md, 8px);
		color: var(--text-primary, var(--foreground));
		font-size: var(--text-base, 0.8125rem);
		font-family: var(--card-font-display, 'Inter', system-ui, sans-serif);
		outline: none;
		transition: all 200ms cubic-bezier(0.16, 1, 0.3, 1);
		box-shadow: inset 0 1px 0 0 rgba(255,255,255,0.03);
	}

	.fp-form-input::placeholder {
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 70%, transparent));
	}

	.fp-form-input:hover {
		border-color: var(--border-default, var(--border));
	}

	.fp-form-input:focus {
		border-color: var(--primary);
		background: var(--surface-0, var(--card));
		box-shadow: inset 0 1px 0 0 rgba(255,255,255,0.03), 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.fp-form-input:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.fp-form-input--mono {
		font-family: var(--card-font-mono, 'JetBrains Mono', 'SF Mono', monospace);
	}

	.fp-form-textarea {
		min-height: 60px;
		resize: vertical;
	}

	.fp-form-actions {
		display: flex;
		gap: var(--space-2, 8px);
		margin-top: var(--space-1, 4px);
	}

	.fp-form-actions .fp-btn {
		flex: 1;
	}

	@media (max-width: 400px) {
		.fp-form-row {
			grid-template-columns: 1fr;
		}
	}

	/* -----------------------------------------------------------------------
	   ProjectCard-specific: item ID badge
	   ----------------------------------------------------------------------- */
	.fp-item-id {
		font-size: var(--text-xs, 0.6875rem);
		font-family: var(--card-font-mono, 'JetBrains Mono', 'SF Mono', monospace);
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 70%, transparent));
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		padding: 2px 6px;
		border-radius: var(--radius-sm, 4px);
	}

	/* -----------------------------------------------------------------------
	   SPINNER
	   ----------------------------------------------------------------------- */
	.fp-spinner {
		width: 14px;
		height: 14px;
		border: 2px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
		border-top-color: currentColor;
		border-radius: 50%;
		animation: fp-spin 0.8s linear infinite;
	}

	@keyframes fp-spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* -----------------------------------------------------------------------
	   REDUCED MOTION
	   ----------------------------------------------------------------------- */
	@media (prefers-reduced-motion: reduce) {
		.fp-form-input {
			transition: none;
		}

		.fp-spinner {
			animation: none;
		}
	}
</style>
