<script lang="ts">
	/**
	 * ProjectCard — Studio-v2 project surface.
	 *
	 * List view: a Studio page header + `.pc`-style project list.
	 * Detail view: page header + project hero + sub-nav (General / Workers /
	 * MCP / Agents / Hooks / Memory) hosting the existing tab components.
	 *
	 * Cross-worker project sync: when GET /projects/overview answers, the list
	 * is every logical project known to any worker, each with worker chips
	 * (ready / missing → set up there / setting up / offline / failed), an
	 * "All workers | On <this worker>" filter and a legend. A project that is
	 * not on this worker is still listed (dimmed icon, "Not on <worker>") and
	 * opens its detail on the Workers tab; the tabs that need a local project
	 * are disabled there with a note. When the overview is not available (an
	 * older worker) the list is this worker's projects with no chips, as before.
	 *
	 * The chrome (header / hero / sub-nav / list) is pure Studio-v2 (app theme
	 * tokens). The inner tab components keep their own styling (they alias the same
	 * theme tokens via card-design-system), so they render on-theme unchanged.
	 */
	import { onDestroy } from 'svelte';
	import { appData as tabs, projects } from '$lib/stores/app-data.svelte';
	import { loadProjectsOverview, patchOverviewWorker, projectsOverview, projectsOverviewSupported } from '$lib/stores/projects-overview';
	import { groups } from '$lib/stores/groups.svelte';
	import {
		Search, Plus, Trash2, FolderOpen, FolderGit2, Folder, Settings, Server, Bot, Zap, Brain,
		ChevronRight, ChevronLeft, Loader2, AlertTriangle, MonitorSmartphone
	} from 'lucide-svelte';
	import Banner from '$lib/components/Banner.svelte';
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import type ProjectMcpTabType from './ProjectMcpTab.svelte';
	import ProjectGeneralTab from './ProjectGeneralTab.svelte';
	import ProjectMcpTab from './ProjectMcpTab.svelte';
	import ProjectAgentsTab from './ProjectAgentsTab.svelte';
	import ProjectHooksTab from './ProjectHooksTab.svelte';
	import ProjectMemoryTab from './ProjectMemoryTab.svelte';
	import ProjectWorkersTab from './ProjectWorkersTab.svelte';
	import ProjectWorkerChips from './ProjectWorkerChips.svelte';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { activeView } from '$lib/stores/layout.svelte';
	import { startProjectSetup, type ProjectKind, type ProjectOverviewRow, type ProjectWorker } from '$lib/api/project-workers';
	import { SETUP_POLL_MS } from '$lib/stores/project-setup';
	import { isOnThisWorker, isRunning, missingCount, selfWorker } from './project-workers-view';
	import './card-design-system.css';

	// View mode: list or detail
	type ViewMode = 'list' | 'detail';
	let viewMode = $state<ViewMode>('list');
	/** The selected row's key: its logical id when known, else this worker's id. */
	let selectedKey = $state<string | null>(null);

	// Detail view tab
	type DetailTab = 'general' | 'workers' | 'mcp' | 'agents' | 'hooks' | 'memory';
	let activeTab = $state<DetailTab>('general');
	/** A chip was clicked: the Workers tab focuses that worker's card. */
	let focusInstallId = $state<string | null>(null);

	const TABS: { id: DetailTab; label: string; icon: typeof Settings; local: boolean }[] = [
		{ id: 'general', label: 'General', icon: Settings, local: true },
		{ id: 'workers', label: 'Workers', icon: MonitorSmartphone, local: false },
		{ id: 'mcp', label: 'MCP Servers', icon: Server, local: true },
		{ id: 'agents', label: 'Agents', icon: Bot, local: true },
		{ id: 'hooks', label: 'Hooks', icon: Zap, local: true },
		{ id: 'memory', label: 'Memory', icon: Brain, local: true }
	];

	// Add project modal state
	let showAddModal = $state(false);

	// Error state
	let error = $state<string | null>(null);

	// Search + worker filter
	let searchQuery = $state('');
	let workerFilter = $state<'all' | 'here'>('all');

	// MCP tab ref (for triggering add from the sub-nav)
	let mcpTabRef = $state<ProjectMcpTabType>();

	// Delete confirmation state (list + detail share these)
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);
	let confirmingRemove = $state(false);

	// Touch/narrow only (list view): which row has its collapsed ⋮ action tray
	// expanded. One open at a time; desktop keeps the always-inline hover rail.
	let actionsOpenId = $state<string | null>(null);

	// The ⋮ toggle and every tray button stop propagation, so any click reaching
	// the window is outside an open tray — collapse it and disarm any pending
	// list delete confirmation.
	function toggleRowActions(e: MouseEvent | KeyboardEvent, id: string) {
		e.stopPropagation();
		actionsOpenId = actionsOpenId === id ? null : id;
	}
	function onWindowClick() {
		if (actionsOpenId !== null) actionsOpenId = null;
		if (viewMode === 'list' && deletingId !== null) deletingId = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			actionsOpenId = null;
			if (viewMode === 'list') deletingId = null;
		}
	}

	// ── Rows: the overview merged with this worker's projects ────────────────

	type LocalProject = (typeof $projects)[number];

	interface ListRow {
		key: string;
		name: string;
		localId: string | null;
		projectV2Id: string | null;
		path: string | null;
		kind: ProjectKind | null;
		remote: string | null;
		defaultBranch: string | null;
		sessionCount: number;
		workers: ProjectWorker[];
		onHere: boolean;
		local: LocalProject | undefined;
	}

	// Ask for the overview each time the page shows (it stays mounted while
	// hidden); live-sync's `projects` refresh keeps it fresh while it is used.
	const shown = $derived(($activeView?.type ?? 'project') === 'project');
	$effect(() => { if (shown) void loadProjectsOverview(); });

	const overviewOn = $derived($projectsOverviewSupported === true);
	const selfName = $derived.by(() => {
		for (const row of $projectsOverview) {
			const self = selfWorker(row.workers);
			if (self) return self.device_name;
		}
		return 'this worker';
	});

	function fromOverview(row: ProjectOverviewRow): ListRow {
		const local = row.local_project_id ? $projects.find((p) => p.id === row.local_project_id) : undefined;
		return {
			key: row.project_v2_id,
			name: local?.name ?? row.name,
			localId: row.local_project_id,
			projectV2Id: row.project_v2_id,
			path: local?.path ?? null,
			kind: row.kind,
			remote: row.remote,
			defaultBranch: row.default_branch,
			sessionCount: row.session_count ?? local?.session_count ?? 0,
			workers: row.workers,
			onHere: isOnThisWorker(row),
			local
		};
	}

	function fromLocal(p: LocalProject): ListRow {
		return {
			key: p.id, name: p.name, localId: p.id, projectV2Id: null, path: p.path, kind: null, remote: null,
			defaultBranch: null, sessionCount: p.session_count ?? 0, workers: [], onHere: true, local: p
		};
	}

	const allRows = $derived.by((): ListRow[] => {
		if (!overviewOn) return $projects.map(fromLocal);
		const rows = $projectsOverview.map(fromOverview);
		// A project this worker just added may not be in the overview yet.
		const covered = new Set(rows.map((r) => r.localId).filter(Boolean));
		for (const p of $projects) if (!covered.has(p.id)) rows.push(fromLocal(p));
		return rows;
	});

	const workerCount = $derived.by(() => {
		const ids = new Set<string>();
		for (const row of $projectsOverview) for (const w of row.workers) ids.add(w.install_id);
		return ids.size;
	});

	// Filtered projects
	const filteredRows = $derived.by(() => {
		let rows = allRows;
		if (overviewOn && workerFilter === 'here') rows = rows.filter((r) => r.onHere);
		if (!searchQuery.trim()) return rows;
		const q = searchQuery.toLowerCase();
		return rows.filter(
			(r) =>
				r.name.toLowerCase().includes(q) ||
				(r.localId ?? '').toLowerCase().includes(q) ||
				(r.path ?? '').toLowerCase().includes(q) ||
				(r.remote ?? '').toLowerCase().includes(q)
		);
	});

	const summary = $derived.by(() => {
		const n = allRows.length;
		const base = `${n} ${n === 1 ? 'project' : 'projects'}`;
		if (!overviewOn || workerCount < 2) return base;
		return `${base} across ${workerCount} workers · viewing from ${selfName}`;
	});

	// While a setup runs somewhere, re-read the overview every 2 s (the
	// resources_changed refresh also lands; this is the fail-safe).
	const anyRunning = $derived(overviewOn && $projectsOverview.some((r) => r.workers.some((w) => isRunning(w))));
	let pollTimer: ReturnType<typeof setInterval> | undefined;
	$effect(() => {
		if (!anyRunning) return;
		pollTimer = setInterval(() => void loadProjectsOverview(), SETUP_POLL_MS);
		return () => { clearInterval(pollTimer); pollTimer = undefined; };
	});
	onDestroy(() => { if (pollTimer) clearInterval(pollTimer); });

	// ── Selection ─────────────────────────────────────────────────────────────

	// Keep the mounted detail shell after deletion so child drafts remain recoverable.
	let retainedRow = $state<ListRow | undefined>();
	const liveRow = $derived(allRows.find((r) => r.key === selectedKey || (!!r.localId && r.localId === selectedKey)));
	$effect(() => { if (liveRow) retainedRow = liveRow; });
	const selectedRow = $derived(liveRow ?? (retainedRow && (retainedRow.key === selectedKey || retainedRow.localId === selectedKey) ? retainedRow : undefined));
	const selectedProject = $derived(selectedRow?.local);
	/** The selected project is not on this worker (no local tabs). */
	const remoteOnly = $derived(!!selectedRow && !selectedRow.localId);
	const showWorkersTab = $derived(overviewOn && !!selectedRow?.projectV2Id);
	const detailMissing = $derived(selectedRow ? missingCount(selectedRow.workers) : 0);

	// When a project gets set up here while its detail is open, its local tabs unlock.
	$effect(() => {
		if (remoteOnly && activeTab !== 'workers') activeTab = 'workers';
	});

	function handleRowClick(row: ListRow, tab?: DetailTab, installId: string | null = null) {
		selectedKey = row.key;
		viewMode = 'detail';
		activeTab = tab ?? (row.localId ? 'general' : 'workers');
		focusInstallId = installId;
		confirmingRemove = false;
	}

	function handleBack() {
		viewMode = 'list';
		selectedKey = null;
		focusInstallId = null;
		confirmingRemove = false;
	}

	/** A dashed chip: set the project up on that worker in its default folder. */
	async function setUpFromChip(row: ListRow, worker: ProjectWorker) {
		if (!row.projectV2Id) return;
		if (!$isAdmin) return handleRowClick(row, 'workers', worker.install_id);
		error = null;
		try {
			const res = await startProjectSetup({ project_v2_id: row.projectV2Id }, { install_id: worker.install_id });
			patchOverviewWorker(row.projectV2Id, worker.install_id, {
				state: res.status === 'done' ? 'ready' : res.status === 'queued_offline' ? 'queued_offline' : 'setting_up',
				setup: { op_id: res.op_id, status: res.status, progress: null }
			});
			void loadProjectsOverview();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = `Couldn't start the setup on ${worker.device_name}: ${err.detail || err.message || 'unknown error'}`;
		}
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

	function rowTag(row: ListRow): string | null {
		if (row.kind === 'git') return row.defaultBranch ? `git · ${row.defaultBranch}` : 'git';
		if (row.kind === 'folder') return 'folder';
		return null;
	}

	function rowMeta(row: ListRow): string {
		const sessions = `${row.sessionCount} session${row.sessionCount === 1 ? '' : 's'}`;
		if (!overviewOn) return row.path ?? '';
		const where = row.remote ?? (row.kind === 'folder' ? 'not a Git repo' : row.path);
		return [where, sessions].filter(Boolean).join(' · ');
	}

	const LEGEND = [
		{ tone: 'ready' as const, text: 'has it' },
		{ tone: 'missing' as const, text: 'click to set up' },
		{ tone: 'busy' as const, text: 'setting up' },
		{ tone: 'offline' as const, text: 'offline' },
		{ tone: 'failed' as const, text: 'failed — open for why' }
	];
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

{#if viewMode === 'detail' && selectedRow}
	{#if selectedRow.localId && !liveRow}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">This project was deleted elsewhere. Any unsaved fields remain available below until you leave.</p>{/if}
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
					<span class="pc-hero-ic" class:dim={remoteOnly}>
						{#if selectedRow.kind === 'git'}<FolderGit2 size={26} strokeWidth={1.6} />{:else}<FolderOpen size={26} strokeWidth={1.6} />{/if}
					</span>
					<div class="pc-hero-id">
						<h2>{selectedRow.name}</h2>
						<div class="pc-hero-path">
							{#if selectedProject}{selectedProject.path}{:else}{selectedRow.remote ?? 'Not on this worker'}{/if}
							{#if selectedRow.remote && selectedProject} · {selectedRow.remote}{/if}
							{#if selectedRow.defaultBranch} · default branch {selectedRow.defaultBranch}{/if}
						</div>
					</div>
					{#if overviewOn && selectedRow.workers.length > 0}
						<div class="pc-hero-chips">
							<ProjectWorkerChips
								workers={selectedRow.workers}
								kind={selectedRow.kind}
								projectName={selectedRow.name}
								onSetup={(w) => setUpFromChip(selectedRow, w)}
								onOpen={(w) => { activeTab = 'workers'; focusInstallId = w.install_id; }}
							/>
						</div>
					{/if}
					{#if selectedProject}
						<div class="pc-hero-acts">
							{#if confirmingRemove}
								<span class="pc-confirm-text">Remove this project{overviewOn ? ` from ${selfName}` : ''}?</span>
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
					{/if}
				</div>

				{#if error}
					<Banner severity="error" message={error} dismissible onDismiss={() => (error = null)} />
				{/if}

				<!-- Sub-nav -->
				<div class="pc-subnav">
					<div class="pc-subtabs" role="tablist" aria-label="Project settings">
						{#each TABS as t (t.id)}
							{#if t.id !== 'workers' || showWorkersTab}
								{@const locked = t.local && remoteOnly}
								<button
									class="pc-subtab"
									class:on={activeTab === t.id}
									class:locked
									role="tab"
									aria-selected={activeTab === t.id}
									aria-disabled={locked}
									title={locked ? `${selectedRow.name} isn't on ${selfName}. Set it up here to change this.` : undefined}
									onclick={() => { if (!locked) { activeTab = t.id; if (t.id !== 'workers') focusInstallId = null; } }}
								>
									<t.icon size={14} strokeWidth={1.7} /><span>{t.label}</span>
									{#if t.id === 'workers' && detailMissing > 0}<span class="pc-count">{detailMissing} missing</span>{/if}
								</button>
							{/if}
						{/each}
					</div>
					{#if activeTab === 'mcp'}
						<button class="pc-btn pc-btn--primary pc-btn--sm" onclick={() => mcpTabRef?.openAddForm()}>
							<Plus size={14} strokeWidth={2} /><span>Add server</span>
						</button>
					{/if}
				</div>

				{#if remoteOnly}
					<p class="pc-note" role="note">
						<strong>{selectedRow.name} isn't on {selfName}.</strong> Its settings, MCP servers, agents, hooks and memory live with each copy; set it up on {selfName} from the card below to change them here.
					</p>
				{/if}

				<!-- Tab body -->
				<div class="pc-tabbody">
					{#if activeTab === 'workers' && showWorkersTab && selectedRow.projectV2Id}
						<ProjectWorkersTab
							projectRef={{ project_v2_id: selectedRow.projectV2Id }}
							projectName={selectedRow.name}
							localProjectId={selectedRow.localId}
							{focusInstallId}
							onChanged={() => void loadProjectsOverview()}
						/>
					{:else if selectedProject}
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
						<Banner severity="error" message={error} dismissible onDismiss={() => (error = null)} />
					{/if}

					<div class="pc-toolbar">
						<div class="pc-search">
							<Search size={14} strokeWidth={1.7} />
							<input type="text" bind:value={searchQuery} placeholder="Search projects…" autocomplete="off" aria-label="Search projects" />
						</div>
						{#if overviewOn && workerCount > 1}
							<div class="pc-seg" role="radiogroup" aria-label="Show projects">
								<button type="button" role="radio" aria-checked={workerFilter === 'all'} class:on={workerFilter === 'all'} onclick={() => (workerFilter = 'all')}>All workers</button>
								<button type="button" role="radio" aria-checked={workerFilter === 'here'} class:on={workerFilter === 'here'} onclick={() => (workerFilter = 'here')}>On {selfName}</button>
							</div>
						{/if}
					</div>

					{#if filteredRows.length === 0}
						<div class="pc-empty">
							{#if searchQuery}
								<Search size={34} strokeWidth={1.25} />
								<p class="pc-empty-title">No projects match "{searchQuery}"</p>
								<button class="pc-link" onclick={() => (searchQuery = '')}>Clear search</button>
							{:else if workerFilter === 'here' && allRows.length > 0}
								<FolderOpen size={34} strokeWidth={1.25} />
								<p class="pc-empty-title">No projects on {selfName} yet</p>
								<p class="pc-empty-sub">Other workers have {allRows.length === 1 ? 'one' : allRows.length}. Open one to set it up here.</p>
								<button class="pc-link" onclick={() => (workerFilter = 'all')}>Show all workers</button>
							{:else}
								<FolderOpen size={34} strokeWidth={1.25} />
								<p class="pc-empty-title">No projects configured yet</p>
								<p class="pc-empty-sub">Projects organize your work into separate workspaces with their own settings.</p>
								<button class="pc-link" onclick={() => (showAddModal = true)}>Add your first project</button>
							{/if}
						</div>
					{:else}
						<div class="pc-list" data-ui-projects={overviewOn ? 'overview' : 'local'}>
							{#each filteredRows as row (row.key)}
								{#if row.localId && deletingId === row.localId}
									<div class="pc-row pc-row--confirm">
										<div class="pc-confirm-body">
											{#if row.sessionCount > 0}
												<span class="pc-confirm-ic"><AlertTriangle size={16} strokeWidth={1.8} /></span>
												<p class="pc-confirm-text">
													Delete <strong>{row.name}</strong>{overviewOn ? ` from ${selfName}` : ''} and its {row.sessionCount} conversation{row.sessionCount === 1 ? '' : 's'}? This can't be undone.
												</p>
											{:else}
												<p class="pc-confirm-text">Delete <strong>{row.name}</strong>{overviewOn ? ` from ${selfName}` : ''}? This can't be undone.</p>
											{/if}
										</div>
										<div class="pc-confirm-actions">
											<button class="pc-btn pc-btn--sm" onclick={(e) => { e.stopPropagation(); deletingId = null; }} disabled={deleteLoading}>Cancel</button>
											<button class="pc-btn pc-btn--sm pc-btn--danger" onclick={(e) => { e.stopPropagation(); handleDeleteProject(); }} disabled={deleteLoading}>
												{#if deleteLoading}<Loader2 size={13} class="pc-spin" />{/if}
												{row.sessionCount > 0 ? 'Delete all' : 'Delete'}
											</button>
										</div>
									</div>
								{:else}
									{@const tag = rowTag(row)}
									<div
										class="pc-row"
										class:pc-row--chips={overviewOn && row.workers.length > 0}
										role="button"
										tabindex="0"
										aria-label="Open {row.name}{row.onHere ? '' : ` (not on ${selfName})`}"
										data-project-key={row.key}
										onclick={() => handleRowClick(row)}
										onkeydown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); handleRowClick(row); } }}
									>
										<span class="pc-ic" class:dim={!row.onHere}>
											{#if row.kind === 'git'}<FolderGit2 size={17} strokeWidth={1.7} />{:else if row.kind === 'folder'}<Folder size={17} strokeWidth={1.7} />{:else}<FolderOpen size={17} strokeWidth={1.7} />{/if}
										</span>
										<div class="pc-body">
											<h4 class="pc-name">
												{row.name}
												{#if tag}<span class="pc-pill">{tag}</span>{/if}
												{#if !row.onHere}<span class="pc-pill pc-pill--warn">Not on {selfName}</span>{/if}
												{#if row.localId && $groups.projects.assignments[row.localId]}
													<span class="pc-badge">{$groups.projects.assignments[row.localId]}</span>
												{/if}
											</h4>
											<p class="pc-path">{rowMeta(row)}</p>
										</div>
										{#if overviewOn && row.workers.length > 0}
											<div class="pc-chips">
												<ProjectWorkerChips
													workers={row.workers}
													kind={row.kind}
													projectName={row.name}
													onSetup={(w) => setUpFromChip(row, w)}
													onOpen={(w) => handleRowClick(row, 'workers', w.install_id)}
												/>
											</div>
										{:else}
											<div class="pc-meta">
												<span class="pc-tag">{row.sessionCount} session{row.sessionCount === 1 ? '' : 's'}</span>
											</div>
										{/if}
										{#if row.localId}
											<RowActionMenu
												open={actionsOpenId === row.key}
												onToggle={(e) => toggleRowActions(e, row.key)}
												label="Project actions"
											>
												<button class="pc-act pc-act--danger" title="Delete" aria-label="Delete {row.name}" onclick={(e) => { e.stopPropagation(); actionsOpenId = null; deletingId = row.localId; }}>
													<Trash2 size={15} strokeWidth={1.7} />
												</button>
											</RowActionMenu>
										{/if}
										<span class="pc-chev"><ChevronRight size={16} strokeWidth={1.7} /></span>
									</div>
								{/if}
							{/each}

							<button class="pc-new" onclick={() => (showAddModal = true)}>
								<span class="pc-new-plus"><Plus size={16} strokeWidth={2.2} /></span>
								<span>New project</span>
							</button>
						</div>
						{#if overviewOn && workerCount > 1}
							<div class="pc-legend">
								<ProjectWorkerChips workers={[]} kind={null} projectName="" legend={LEGEND} />
							</div>
						{/if}
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}

<!-- Add Project Modal (shared across all modes) -->
<AddProjectModal open={showAddModal} onClose={() => (showAddModal = false)} onCreated={() => void loadProjectsOverview()} />

<style>
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
	.pc-btn--danger { color: var(--destructive); border-color: color-mix(in oklab, var(--destructive) 35%, var(--border)); }
	.pc-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

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

	/* Row-hover reveal of the action rail is signalled to RowActionMenu through the
	   inherited --ram-reveal custom property (0 hidden / 1 shown); the component
	   owns the button opacity + the touch/narrow ⋮ collapse + slide-out. */
	.pc-row { --ram-reveal: 0; }
	.pc-row:hover, .pc-row:focus-within { --ram-reveal: 1; }
	.pc-act { display: grid; place-items: center; width: 28px; height: 28px; background: transparent; border: 1px solid transparent; border-radius: 7px; color: var(--muted-foreground); cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.pc-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.pc-chev { display: grid; place-items: center; color: var(--muted-foreground); flex-shrink: 0; }

	/* Inline confirm row */
	.pc-row--confirm { justify-content: space-between; cursor: default; background: color-mix(in oklab, var(--destructive) 6%, var(--card)); flex-wrap: wrap; }
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


	/* ── Cross-worker sync ── */
	.pc-ic.dim, .pc-hero-ic.dim { background: color-mix(in oklch, var(--muted-foreground) 14%, transparent); color: var(--muted-foreground); }
	.pc-hero-chips { flex: 0 1 auto; min-width: 0; max-width: 46%; }
	.pc-pill { flex-shrink: 0; font-family: var(--font-sans, inherit); font-size: 0.66rem; font-weight: 500; letter-spacing: 0; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted-foreground); }
	.pc-pill--warn { color: var(--warning); border-color: color-mix(in oklch, var(--warning) 45%, transparent); }
	.pc-chips { flex: 0 1 auto; min-width: 0; max-width: 48%; }
	.pc-legend { margin-top: -2px; font-size: 0.75rem; }
	.pc-seg { display: inline-flex; flex-shrink: 0; border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px); overflow: hidden; background: var(--card); }
	.pc-seg button { height: 32px; padding: 0 12px; border: none; background: transparent; color: var(--muted-foreground); font: inherit; font-size: 0.78rem; font-weight: 500; cursor: pointer; white-space: nowrap; transition: color 120ms ease, background-color 120ms ease; }
	.pc-seg button + button { border-left: 1px solid var(--border); }
	.pc-seg button:hover { color: var(--foreground); }
	.pc-seg button.on { color: var(--foreground); background: color-mix(in oklch, var(--primary) 16%, transparent); }
	.pc-seg button:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.pc-subtab.locked { opacity: 0.45; cursor: not-allowed; }
	.pc-subtab.locked:hover { color: var(--muted-foreground); }
	.pc-subtab:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; border-radius: 6px; }
	.pc-count { font-size: 0.66rem; font-weight: 600; padding: 0 6px; border-radius: 999px; background: color-mix(in oklch, var(--warning) 22%, transparent); color: var(--warning); }
	.pc-note { margin: 12px 0 0; padding: 10px 12px; border-radius: calc(var(--radius, 0.75rem) - 2px); background: color-mix(in oklch, var(--warning) 9%, transparent); border: 1px solid color-mix(in oklch, var(--warning) 35%, transparent); font-size: 0.78rem; color: var(--foreground); }

	@media (max-width: 639px) {
		.pc-wrap { padding: 18px 16px 24px; }
		.pc-search { max-width: none; }
		.pc-meta { display: none; }
		.pc-hero { flex-wrap: wrap; }
		.pc-hero-acts { margin-left: 0; width: 100%; }
		.pc-hero-chips { max-width: none; width: 100%; }
		.pc-hero-chips :global(.wchips) { justify-content: flex-start; }
		.pc-toolbar { flex-wrap: wrap; }
		.pc-seg { width: 100%; }
		.pc-seg button { flex: 1; height: 36px; }
		/* Chips drop under the name, aligned with it. */
		.pc-row--chips { flex-wrap: wrap; row-gap: 8px; }
		.pc-row--chips .pc-body { flex: 1 1 0; min-width: 0; }
		.pc-row--chips .pc-chips { order: 5; flex: 1 0 100%; max-width: none; padding-left: 50px; }
		.pc-row--chips .pc-chips :global(.wchips) { justify-content: flex-start; }
		.pc-row--chips :global(.wchip) { min-height: 28px; }
	}
</style>
