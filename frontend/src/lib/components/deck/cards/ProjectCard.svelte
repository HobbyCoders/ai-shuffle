<script lang="ts">
	/**
	 * ProjectCard — Studio-v2 project surface.
	 *
	 * List view: a Studio page header + `.pc`-style project list.
	 * Detail view: page header + project hero + sub-nav (General / MCP / Agents /
	 * Hooks / Memory) hosting the existing tab components.
	 *
	 * The chrome (header / hero / sub-nav / list) is pure Studio-v2 (app theme
	 * tokens). The inner tab components keep their own styling (they alias the same
	 * theme tokens via card-design-system), so they render on-theme unchanged.
	 */
	import { appData as tabs, projects } from '$lib/stores/app-data.svelte';
	import { groups } from '$lib/stores/groups.svelte';
	import {
		Search, Plus, Trash2, FolderOpen, X, Settings, Server, Bot, Zap, Brain,
		ChevronRight, ChevronLeft, Loader2, AlertTriangle
	} from 'lucide-svelte';
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import type ProjectMcpTabType from './ProjectMcpTab.svelte';
	import ProjectGeneralTab from './ProjectGeneralTab.svelte';
	import ProjectMcpTab from './ProjectMcpTab.svelte';
	import ProjectAgentsTab from './ProjectAgentsTab.svelte';
	import ProjectHooksTab from './ProjectHooksTab.svelte';
	import ProjectMemoryTab from './ProjectMemoryTab.svelte';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
	}

	let { mobile = false }: Props = $props();

	// View mode: list or detail
	type ViewMode = 'list' | 'detail';
	let viewMode = $state<ViewMode>('list');
	let selectedProjectId = $state<string | null>(null);

	// Detail view tab
	type DetailTab = 'general' | 'mcp' | 'agents' | 'hooks' | 'memory';
	let activeTab = $state<DetailTab>('general');

	const TABS: { id: DetailTab; label: string; icon: typeof Settings }[] = [
		{ id: 'general', label: 'General', icon: Settings },
		{ id: 'mcp', label: 'MCP Servers', icon: Server },
		{ id: 'agents', label: 'Agents', icon: Bot },
		{ id: 'hooks', label: 'Hooks', icon: Zap },
		{ id: 'memory', label: 'Memory', icon: Brain }
	];

	// Add project modal state
	let showAddModal = $state(false);

	// Error state
	let error = $state<string | null>(null);

	// Search state
	let searchQuery = $state('');

	// MCP tab ref (for triggering add from the sub-nav)
	let mcpTabRef = $state<ProjectMcpTabType>();

	// Delete confirmation state (list + detail share these)
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);
	let confirmingRemove = $state(false);

	// Get selected project
	const selectedProject = $derived($projects.find((p) => p.id === selectedProjectId));

	// Filtered projects
	const filteredProjects = $derived.by(() => {
		if (!searchQuery.trim()) return $projects;
		const q = searchQuery.toLowerCase();
		return $projects.filter(
			(p) =>
				p.name.toLowerCase().includes(q) ||
				p.id.toLowerCase().includes(q) ||
				p.path.toLowerCase().includes(q)
		);
	});

	const summary = $derived(
		`${$projects.length} ${$projects.length === 1 ? 'project' : 'projects'}`
	);

	function handleProjectClick(projectId: string) {
		selectedProjectId = projectId;
		viewMode = 'detail';
		activeTab = 'general';
		confirmingRemove = false;
	}

	function handleBack() {
		viewMode = 'list';
		selectedProjectId = null;
		confirmingRemove = false;
	}

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

	async function handleRemoveFromDetail() {
		if (!selectedProject) return;
		deletingId = selectedProject.id;
		await handleDeleteProject();
		if (!error) handleBack();
		confirmingRemove = false;
	}
</script>

{#if viewMode === 'detail' && selectedProject}
	<!-- ─────────────────────────── DETAIL ─────────────────────────── -->
	<div class="pc">
		<div class="pc-scroll">
			<div class="pc-wrap">
				<button class="pc-back" onclick={handleBack}>
					<ChevronLeft size={15} strokeWidth={2} /><span>Projects</span>
				</button>

				<div class="pc-head">
					<StudioPageHeader eyebrow="Project" title="Project" accent="settings" />
					<p class="pc-sub">Configuration scoped to the selected project.</p>
				</div>

				<!-- Hero -->
				<div class="pc-hero">
					<span class="pc-hero-ic"><FolderOpen size={26} strokeWidth={1.6} /></span>
					<div class="pc-hero-id">
						<h2>{selectedProject.name}</h2>
						<div class="pc-hero-path">{selectedProject.path}</div>
					</div>
					<div class="pc-hero-acts">
						{#if confirmingRemove}
							<span class="pc-confirm-text">Remove this project?</span>
							<button class="pc-btn pc-btn--sm" onclick={() => (confirmingRemove = false)} disabled={deleteLoading}>Cancel</button>
							<button class="pc-btn pc-btn--sm pc-btn--danger" onclick={handleRemoveFromDetail} disabled={deleteLoading}>
								{#if deleteLoading}<Loader2 size={13} class="pc-spin" />{/if}Remove
							</button>
						{:else}
							<button class="pc-btn pc-btn--sm pc-btn--danger" onclick={() => (confirmingRemove = true)}>
								<Trash2 size={14} strokeWidth={1.8} /><span>Remove</span>
							</button>
						{/if}
					</div>
				</div>

				{#if error}
					<div class="pc-error">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)} aria-label="Dismiss"><X size={14} /></button>
					</div>
				{/if}

				<!-- Sub-nav -->
				<div class="pc-subnav">
					<div class="pc-subtabs">
						{#each TABS as t}
							<button class="pc-subtab" class:on={activeTab === t.id} onclick={() => (activeTab = t.id)}>
								<t.icon size={14} strokeWidth={1.7} /><span>{t.label}</span>
							</button>
						{/each}
					</div>
					{#if activeTab === 'mcp'}
						<button class="pc-btn pc-btn--primary pc-btn--sm" onclick={() => mcpTabRef?.openAddForm()}>
							<Plus size={14} strokeWidth={2} /><span>Add server</span>
						</button>
					{/if}
				</div>

				<!-- Tab body -->
				<div class="pc-tabbody">
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
				</div>
			</div>
		</div>
	</div>
{:else}
	<!-- ─────────────────────────── LIST ─────────────────────────── -->
	<div class="pc">
		<div class="pc-scroll">
			<div class="pc-wrap">
				<div class="pc-head">
					<StudioPageHeader eyebrow="Workspace" title="Your" accent="projects">
						{#snippet actions()}
							<button class="pc-btn pc-btn--primary" onclick={() => (showAddModal = true)}>
								<Plus size={15} strokeWidth={2} /><span>New project</span>
							</button>
						{/snippet}
					</StudioPageHeader>
					<p class="pc-sub">{summary}</p>
				</div>

				<div class="pc-list-body">
					{#if error}
						<div class="pc-error">
							<span class="flex-1">{error}</span>
							<button onclick={() => (error = null)} aria-label="Dismiss"><X size={14} /></button>
						</div>
					{/if}

					<div class="pc-toolbar">
						<div class="pc-search">
							<Search size={14} strokeWidth={1.7} />
							<input type="text" bind:value={searchQuery} placeholder="Search projects…" autocomplete="off" />
						</div>
					</div>

					{#if filteredProjects.length === 0}
						<div class="pc-empty">
							{#if searchQuery}
								<Search size={34} strokeWidth={1.25} />
								<p class="pc-empty-title">No projects match "{searchQuery}"</p>
								<button class="pc-link" onclick={() => (searchQuery = '')}>Clear search</button>
							{:else}
								<FolderOpen size={34} strokeWidth={1.25} />
								<p class="pc-empty-title">No projects configured yet</p>
								<p class="pc-empty-sub">Projects organize your work into separate workspaces with their own settings.</p>
								<button class="pc-link" onclick={() => (showAddModal = true)}>Add your first project</button>
							{/if}
						</div>
					{:else}
						<div class="pc-list">
							{#each filteredProjects as project (project.id)}
								{#if deletingId === project.id}
									<div class="pc-row pc-row--confirm">
										<div class="pc-confirm-body">
											{#if project.session_count && project.session_count > 0}
												<span class="pc-confirm-ic"><AlertTriangle size={16} strokeWidth={1.8} /></span>
												<p class="pc-confirm-text">
													Delete <strong>{project.name}</strong> and its {project.session_count} conversation{project.session_count === 1 ? '' : 's'}? This can't be undone.
												</p>
											{:else}
												<p class="pc-confirm-text">Delete <strong>{project.name}</strong>? This can't be undone.</p>
											{/if}
										</div>
										<div class="pc-confirm-actions">
											<button class="pc-btn pc-btn--sm" onclick={() => (deletingId = null)} disabled={deleteLoading}>Cancel</button>
											<button class="pc-btn pc-btn--sm pc-btn--danger" onclick={handleDeleteProject} disabled={deleteLoading}>
												{#if deleteLoading}<Loader2 size={13} class="pc-spin" />{/if}
												{project.session_count && project.session_count > 0 ? 'Delete all' : 'Delete'}
											</button>
										</div>
									</div>
								{:else}
									<div
										class="pc-row"
										role="button"
										tabindex="0"
										onclick={() => handleProjectClick(project.id)}
										onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleProjectClick(project.id); } }}
									>
										<span class="pc-ic"><FolderOpen size={17} strokeWidth={1.7} /></span>
										<div class="pc-body">
											<h4 class="pc-name">
												{project.name}
												{#if $groups.projects.assignments[project.id]}
													<span class="pc-badge">{$groups.projects.assignments[project.id]}</span>
												{/if}
											</h4>
											<p class="pc-path">{project.path}</p>
										</div>
										<div class="pc-meta">
											<span class="pc-tag">{project.session_count || 0} session{project.session_count === 1 ? '' : 's'}</span>
										</div>
										<div class="pc-actions" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
											<button class="pc-act pc-act--danger" title="Delete" onclick={(e) => { e.stopPropagation(); deletingId = project.id; }}>
												<Trash2 size={15} strokeWidth={1.7} />
											</button>
										</div>
										<span class="pc-chev"><ChevronRight size={16} strokeWidth={1.7} /></span>
									</div>
								{/if}
							{/each}

							<button class="pc-new" onclick={() => (showAddModal = true)}>
								<span class="pc-new-plus"><Plus size={16} strokeWidth={2.2} /></span>
								<span>New project</span>
							</button>
						</div>
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}

<!-- Add Project Modal (shared across all modes) -->
<AddProjectModal open={showAddModal} onClose={() => (showAddModal = false)} />

<style>
	.flex-1 { flex: 1; }

	.pc {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}
	.pc-wrap {
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.pc-scroll { flex: 1; min-height: 0; overflow-y: auto; }
	.pc-wrap { padding: 22px 28px 36px; }

	.pc-back {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		margin-bottom: 12px;
		padding: 5px 9px 5px 6px;
		background: none;
		border: none;
		border-radius: 7px;
		color: var(--muted-foreground);
		font-size: 0.8rem;
		font-weight: 600;
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.pc-back:hover { color: var(--foreground); background: var(--hover-overlay); }

	.pc-head { flex-shrink: 0; }
	.pc-sub { margin: 8px 0 0; font-size: 0.8125rem; color: var(--muted-foreground); }

	/* Hero */
	.pc-hero {
		display: flex;
		align-items: center;
		gap: 16px;
		margin-top: 20px;
		padding: 16px 18px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
	}
	.pc-hero-ic {
		display: grid;
		place-items: center;
		width: 52px;
		height: 52px;
		flex-shrink: 0;
		border-radius: 14px;
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		color: var(--primary);
	}
	.pc-hero-id { min-width: 0; flex: 1; }
	.pc-hero-id h2 {
		margin: 0;
		font-family: var(--font-display, Georgia, serif);
		font-weight: 800;
		font-size: 1.35rem;
		letter-spacing: -0.01em;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pc-hero-path {
		margin-top: 4px;
		font-family: var(--font-mono, 'JetBrains Mono', monospace);
		font-size: 0.75rem;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pc-hero-acts { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
	.pc-confirm-text { font-size: 0.8rem; color: var(--foreground); margin: 0; }

	/* Sub-nav (underline tabs, matches Build) */
	.pc-subnav {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-top: 22px;
		border-bottom: 1px solid var(--border);
	}
	.pc-subtabs { display: flex; gap: 2px; flex-wrap: wrap; }
	.pc-subtab {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 7px;
		padding: 10px 12px;
		background: none;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		transition: color 120ms ease;
	}
	.pc-subtab:hover { color: var(--foreground); }
	.pc-subtab.on { color: var(--foreground); }
	.pc-subtab.on::after {
		content: '';
		position: absolute;
		left: 8px;
		right: 8px;
		bottom: -1px;
		height: 2px;
		background: var(--primary);
		border-radius: 2px;
	}

	.pc-tabbody { padding-top: 4px; }

	/* Buttons */
	.pc-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, border-color 120ms ease, filter 120ms ease;
	}
	.pc-btn:hover { background: var(--hover-overlay); }
	.pc-btn:disabled { opacity: 0.55; cursor: not-allowed; }
	.pc-btn--sm { height: 30px; padding: 0 11px; font-size: 0.78rem; }
	.pc-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.pc-btn--primary:hover { filter: brightness(1.08); background: var(--primary); }
	.pc-btn--danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 35%, var(--border)); }
	.pc-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Error */
	.pc-error {
		display: flex; align-items: center; gap: 8px; margin-top: 16px; padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive); font-size: 0.8125rem;
	}
	.pc-error button { background: transparent; border: none; color: var(--destructive); cursor: pointer; padding: 2px; display: grid; place-items: center; }

	/* List */
	.pc-list-body { display: flex; flex-direction: column; gap: 14px; margin-top: 18px; }
	.pc-toolbar { display: flex; align-items: center; gap: 12px; }
	.pc-search { position: relative; display: flex; align-items: center; flex: 1; max-width: 320px; }
	.pc-search :global(svg) { position: absolute; left: 11px; color: var(--muted-foreground); pointer-events: none; }
	.pc-search input {
		width: 100%; height: 34px; padding: 0 12px 0 32px;
		background: var(--input-bg, var(--card)); border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground); font-size: 0.8125rem; outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.pc-search input::placeholder { color: var(--muted-foreground); }
	.pc-search input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }

	.pc-list { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius, 0.75rem); overflow: hidden; background: var(--card); }
	.pc-row { display: flex; align-items: center; gap: 14px; padding: 13px 16px; min-height: 60px; border-bottom: 1px solid var(--border-subtle, var(--border)); cursor: pointer; transition: background-color 120ms ease; }
	.pc-row:last-child { border-bottom: none; }
	.pc-row:hover { background: var(--hover-overlay); }
	.pc-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.pc-ic { display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: calc(var(--radius, 0.75rem) - 4px); background: color-mix(in oklch, var(--primary) 12%, transparent); color: var(--primary); }
	.pc-body { flex: 1; min-width: 0; }
	.pc-name { display: flex; align-items: center; gap: 8px; margin: 0 0 3px; font-family: var(--font-display, Georgia, serif); font-size: 0.9rem; font-weight: 700; letter-spacing: -0.01em; color: var(--foreground); flex-wrap: wrap; }
	.pc-path { margin: 0; font-family: var(--font-mono, 'JetBrains Mono', monospace); font-size: 0.72rem; color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.pc-badge { flex-shrink: 0; font-size: 0.6rem; font-weight: 600; letter-spacing: 0.03em; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 50%, transparent); padding: 2px 7px; border-radius: 999px; }

	.pc-meta { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
	.pc-tag { font-family: var(--font-mono, monospace); font-size: 0.6875rem; font-weight: 500; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 45%, transparent); padding: 2px 8px; border-radius: 6px; white-space: nowrap; }

	.pc-actions { display: flex; gap: 2px; flex-shrink: 0; opacity: 0; transition: opacity 120ms ease; }
	.pc-row:hover .pc-actions, .pc-row:focus-within .pc-actions { opacity: 1; }
	/* Touch / mobile-width: actions can't be hover-revealed — keep them visible.
	   (hover: none) covers real touch devices incl. unfolded foldables; 1080px
	   matches the app-wide mobile cutoff so the desktop resize-test path works too. */
	@media (hover: none), (max-width: 1080px) {
		.pc-actions { opacity: 1; }
	}
	.pc-act { display: grid; place-items: center; width: 28px; height: 28px; background: transparent; border: 1px solid transparent; border-radius: 7px; color: var(--muted-foreground); cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.pc-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.pc-chev { display: grid; place-items: center; color: var(--muted-foreground); flex-shrink: 0; }

	/* Inline confirm row */
	.pc-row--confirm { justify-content: space-between; cursor: default; background: color-mix(in oklch, var(--destructive) 6%, var(--card)); flex-wrap: wrap; }
	.pc-confirm-body { display: flex; align-items: center; gap: 8px; min-width: 0; }
	.pc-confirm-ic { color: var(--warning, #d97706); flex-shrink: 0; display: grid; place-items: center; }
	.pc-confirm-actions { display: flex; gap: 8px; flex-shrink: 0; }

	/* New row */
	.pc-new { display: flex; align-items: center; justify-content: center; gap: 8px; padding: 13px 16px; min-height: 52px; background: transparent; border: none; border-top: 1px dashed color-mix(in oklch, var(--muted-foreground) 35%, transparent); color: var(--muted-foreground); font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.pc-new:hover { color: var(--foreground); background: var(--hover-overlay); }
	.pc-new-plus { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: var(--primary); color: var(--primary-foreground); }

	/* Empty */
	.pc-empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 48px 24px; text-align: center; color: var(--muted-foreground); }
	.pc-empty-title { margin: 4px 0 0; font-size: 0.875rem; color: var(--foreground); }
	.pc-empty-sub { margin: 0; font-size: 0.78rem; max-width: 360px; }
	.pc-link { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.8125rem; margin-top: 4px; }
	.pc-link:hover { text-decoration: underline; }

	:global(.pc-spin) { animation: pc-spin 1s linear infinite; }
	@keyframes pc-spin { to { transform: rotate(360deg); } }

	@media (max-width: 639px) {
		.pc-wrap { padding: 18px 16px 24px; }
		.pc-search { max-width: none; }
		.pc-meta { display: none; }
		.pc-actions { opacity: 1; }
		.pc-hero { flex-wrap: wrap; }
		.pc-hero-acts { margin-left: 0; width: 100%; }
	}
</style>
