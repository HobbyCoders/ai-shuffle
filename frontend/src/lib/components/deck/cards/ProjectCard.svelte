<script lang="ts">
	/**
	 * ProjectCard - Project management card
	 *
	 * Features:
	 * - Project list with name, path display
	 * - Click project to view detail with tabs:
	 *   - General (name edit, ID, path, group assignment)
	 *   - MCP Servers (local and project scope)
	 *   - Agents (read-only list from .claude/agents/)
	 *   - Hooks (read-only list with toggle/test/delete)
	 *   - Memory (CLAUDE.md file management)
	 * - Group badge display
	 * - Delete with inline confirmation
	 * - Add project via shared modal with directory browser
	 * - Search/filter for many projects
	 */
	import { appData as tabs, projects } from '$lib/stores/app-data.svelte';
	import { groups } from '$lib/stores/groups.svelte';
	import { Search, Plus, Trash2, FolderOpen, X, Settings, Server, Bot, Zap, MessageSquare, Brain, ChevronRight } from 'lucide-svelte';
	import type ProjectMcpTabType from './ProjectMcpTab.svelte';
	import ProjectGeneralTab from './ProjectGeneralTab.svelte';
	import ProjectMcpTab from './ProjectMcpTab.svelte';
	import ProjectAgentsTab from './ProjectAgentsTab.svelte';
	import ProjectHooksTab from './ProjectHooksTab.svelte';
	import ProjectMemoryTab from './ProjectMemoryTab.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// View mode: list or detail
	type ViewMode = 'list' | 'detail';
	let viewMode = $state<ViewMode>('list');
	let selectedProjectId = $state<string | null>(null);

	// Detail view tab
	type DetailTab = 'general' | 'mcp' | 'agents' | 'hooks' | 'memory';
	let activeTab = $state<DetailTab>('general');

	// Add project modal state
	let showAddModal = $state(false);

	// Error state
	let error = $state<string | null>(null);

	// Search state
	let searchQuery = $state('');

	// MCP tab ref (for triggering add from footer)
	let mcpTabRef = $state<ProjectMcpTabType>();

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
		activeTab = 'general';
	}

	// Back to list view
	function handleBack() {
		viewMode = 'list';
		selectedProjectId = null;
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


</script>

{#snippet deleteConfirmation(project: typeof $projects[0])}
	<div class="card-list-item card-list-item--deleting fp-list-item fp-list-item--deleting">
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

{#if viewMode === 'detail' && selectedProject}
		<!-- Detail View -->
		<FeaturePageLayout
			icon={FolderOpen}
			title="Projects"
			breadcrumbs={[
				{ label: 'Projects', onclick: handleBack },
				{ label: selectedProject.name }
			]}
		>
			{#snippet headerTabs()}
				<div class="fp-tab-bar">
					<button class="fp-tab" class:active={activeTab === 'general'} onclick={() => (activeTab = 'general')}>
						<Settings size={14} /> General
					</button>
					<button class="fp-tab" class:active={activeTab === 'mcp'} onclick={() => (activeTab = 'mcp')}>
						<Server size={14} /> MCP Servers
					</button>
					<button class="fp-tab" class:active={activeTab === 'agents'} onclick={() => (activeTab = 'agents')}>
						<Bot size={14} /> Agents
					</button>
					<button class="fp-tab" class:active={activeTab === 'hooks'} onclick={() => (activeTab = 'hooks')}>
						<Zap size={14} /> Hooks
					</button>
					<button class="fp-tab" class:active={activeTab === 'memory'} onclick={() => (activeTab = 'memory')}>
						<Brain size={14} /> Memory
					</button>
				</div>
			{/snippet}

			{#snippet footer()}
				{#if activeTab === 'mcp'}
					<button class="fp-btn fp-btn--primary" onclick={() => mcpTabRef?.openAddForm()}>
						<Plus size={14} /> <span class="btn-label">Add MCP Server</span>
					</button>
				{/if}
			{/snippet}

			{#if activeTab === 'general'}
				<ProjectGeneralTab projectId={selectedProject.id} />
			{:else if activeTab === 'mcp'}
				<ProjectMcpTab bind:this={mcpTabRef} projectId={selectedProject.id} />
			{:else if activeTab === 'agents'}
				<ProjectAgentsTab projectId={selectedProject.id} />
			{:else if activeTab === 'hooks'}
				<ProjectHooksTab projectId={selectedProject.id} />
			{:else if activeTab === 'memory'}
				<ProjectMemoryTab projectId={selectedProject.id} />
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
			{#snippet footer()}
				<button class="fp-btn fp-btn--primary" onclick={() => (showAddModal = true)}>
					<Plus size={14} /> <span class="btn-label">New Project</span>
				</button>
			{/snippet}

			<div class="fp-search-bar">
				<div class="fph-search-wrapper">
					<Search size={14} />
					<input type="text" bind:value={searchQuery} placeholder="Search projects..." class="fph-search-input" />
				</div>
			</div>

			<!-- Error banner if any -->
			{#if error}
				<div class="fp-error-banner">
					<span class="flex-1">{error}</span>
					<button onclick={() => (error = null)}>
						<X size={14} />
					</button>
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
					{#each filteredProjects() as project, index (project.id)}
						{#if index > 0}<div class="fp-list-divider"></div>{/if}
						{#if deletingId === project.id}
							{@render deleteConfirmation(project)}
						{:else}
							<div
								class="fp-list-item"
								onclick={() => handleProjectClick(project.id)}
								role="button"
								tabindex="0"
								onkeydown={(e) => e.key === 'Enter' && handleProjectClick(project.id)}
							>
								<div class="fp-item-indicator"></div>
								<div class="fp-item-content">
									<div class="fp-item-header">
										<span class="fp-item-name">{project.name}</span>
										{#if $groups.projects.assignments[project.id]}
											<span class="fp-item-badge">{$groups.projects.assignments[project.id]}</span>
										{/if}
									</div>
									<span class="fp-item-id">{project.path}</span>
									<div class="fp-item-meta">
										<span class="fp-meta-chip fp-meta-model">{project.session_count || 0} sessions</span>
									</div>
								</div>
								<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
									<button
										class="fp-action-btn fp-action-btn--destructive"
										onclick={(e) => { e.stopPropagation(); deletingId = project.id; }}
										title="Delete"
									>
										<Trash2 size={14} />
									</button>
								</div>
								<div class="fp-item-chevron"><ChevronRight size={16} /></div>
							</div>
						{/if}
					{/each}
				</div>
			{/if}
		</FeaturePageLayout>
	{/if}

<!-- Add Project Modal (shared across all modes) -->
<AddProjectModal
	open={showAddModal}
	onClose={() => (showAddModal = false)}
/>

<style>
	/* ═══════════════════════════════════════════════════════════════════════════
	   NON-EMBEDDED (ORIGINAL) STYLES
	   ═══════════════════════════════════════════════════════════════════════════ */

	/* Text utilities */
	.flex-1 {
		flex: 1;
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
		.fp-spinner {
			animation: none;
		}
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

</style>
