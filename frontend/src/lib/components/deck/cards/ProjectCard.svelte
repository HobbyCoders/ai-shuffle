<script lang="ts">
	/** Projects uses the shared master/detail view; project and worker actions stay local to this page. */
	import { onDestroy, onMount, untrack } from 'svelte';
	import { ViewShell, ViewPanel, ViewMain, PanelSearch, PanelIconButton, DetailPane, EmptyDetail, ListItem, Segmented, InlineBanner, RowGroup, SettingRow } from '$lib/components/layout/view';
	import { platform } from '$lib/stores/platform.svelte';
	import { appData as tabs, projects } from '$lib/stores/app-data.svelte';
	import { loadProjectsOverview, patchOverviewWorker, projectsOverview, projectsOverviewSupported } from '$lib/stores/projects-overview';
	import { Plus, FolderOpen, FolderGit2, Settings, Server, Bot, Zap, Brain, Loader2, MonitorSmartphone } from 'lucide-svelte';
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
	import './project-tabs.css';

	let stacked = $state(false);
	let mounted = $state(false);
	onMount(() => { mounted = true; });
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
		{ id: 'mcp', label: 'MCP servers', icon: Server, local: true },
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
		activeTab = tab ?? (row.localId ? 'general' : 'workers');
		focusInstallId = installId;
		confirmingRemove = false;
	}

	function handleBack() {
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

	function rowMeta(row: ListRow): string {
		return [row.defaultBranch, `${row.sessionCount} session${row.sessionCount === 1 ? '' : 's'}`, !row.onHere ? `Not on ${selfName}` : ''].filter(Boolean).join(' · ');
	}

	// Wait for the actual viewport before desktop auto-selection. Phone opens the list.
	let autoSelected = false;
	$effect(() => {
		if (!shown || !mounted || autoSelected || !allRows.length) return;
		if (platform.isMobile || window.innerWidth <= 600) { autoSelected = true; return; }
		autoSelected = true;
		untrack(() => { if (!selectedKey) handleRowClick(allRows[0]); });
	});
	let handledItem: string | null = null;
	$effect(() => {
		const id = shown ? $activeView?.itemId : null;
		if (!id) { handledItem = null; return; }
		if (id === handledItem) return;
		const row = allRows.find(r => r.key === id || r.localId === id);
		if (row) { handledItem = id; untrack(() => handleRowClick(row)); }
	});
	const detailTabs = $derived(TABS.filter(t => t.id !== 'workers' || showWorkersTab)
		.map(t => ({ id: t.id, label: t.label, disabled: t.local && remoteOnly,
			title: t.local && remoteOnly ? `${selectedRow?.name} isn't on ${selfName}. Set it up here to change this.` : undefined,
			count: t.id === 'workers' && detailMissing > 0 ? `${detailMissing} missing` : undefined })));

</script>

<ViewShell label="Projects" detailOpen={!!selectedRow} onBack={handleBack} bind:stacked>
	{#snippet panel()}
		<ViewPanel title="Projects" count={allRows.length}>
			{#snippet actions()}<PanelIconButton label="New project" icon={Plus} onclick={() => (showAddModal = true)} />{/snippet}
			{#snippet search()}<PanelSearch bind:value={searchQuery} label="Search projects" placeholder="Search projects" />{/snippet}
			{#snippet filters()}
				{#if overviewOn && workerCount > 1}
					<Segmented label="Show projects" size="sm" bind:value={workerFilter} options={[{ value: 'all', label: 'All workers' }, { value: 'here', label: `On ${selfName}` }]} />
				{/if}
			{/snippet}
			<div data-ui-projects={overviewOn ? 'overview' : 'local'}>
				{#each filteredRows as row (row.key)}
					<div data-project-key={row.key}>
						<ListItem id={row.key} name={row.name} summary={rowMeta(row)} icon={row.kind === 'git' ? FolderGit2 : FolderOpen}
							status={row.onHere ? 'ok' : 'off'} selected={selectedRow?.key === row.key}
							ariaLabel={`Open ${row.name}${row.onHere ? '' : ` (not on ${selfName})`}`} onselect={() => handleRowClick(row)} />
					</div>
				{:else}
					<EmptyDetail title={searchQuery ? `No projects match "${searchQuery}"` : workerFilter === 'here' && allRows.length ? `No projects on ${selfName} yet` : 'No projects configured yet'} message="Projects keep your work and settings together.">
						{#if searchQuery}<button class="st-btn" onclick={() => (searchQuery = '')}>Clear search</button>
						{:else if workerFilter === 'here' && allRows.length}<button class="st-btn" onclick={() => (workerFilter = 'all')}>Show all workers</button>
						{:else}<button class="st-btn primary" onclick={() => (showAddModal = true)}>New project</button>{/if}
					</EmptyDetail>
				{/each}
			</div>
		</ViewPanel>
	{/snippet}
	{#snippet main(back)}
		{#if selectedRow}
				{#snippet projectDangerRows()}
					{#if selectedProject}
						<SettingRow wrapHint label="Remove project" hint={confirmingRemove ? `Remove ${selectedRow.name}${overviewOn ? ` from ${selfName}` : ''} and its ${selectedRow.sessionCount} conversations? This cannot be undone.` : 'Removes the project and its conversations from this worker. Files stay on disk.'}>
							{#if confirmingRemove}<button class="st-btn" disabled={deleteLoading} onclick={() => (confirmingRemove = false)}>Cancel</button>
								<button class="st-btn danger" disabled={deleteLoading} onclick={handleRemoveFromDetail}>{deleteLoading ? 'Removing…' : 'Remove'}</button>
							{:else}<button class="st-btn danger" title={`Remove ${selectedRow.name} and its chats from this worker`} onclick={() => (confirmingRemove = true)}>Remove project and chats</button>{/if}
						</SettingRow>
					{/if}
				{/snippet}
			<DetailPane width="reading" name={selectedRow.name} icon={selectedRow.kind === 'git' ? FolderGit2 : FolderOpen} onBack={back} backLabel="Projects"
				dangerRows={selectedProject ? projectDangerRows : undefined} tabs={detailTabs} tab={activeTab} onTabChange={(tab) => { activeTab = tab as DetailTab; focusInstallId = null; }}>
				{#snippet topStatus()}{/snippet}
				{#snippet subtitle()}
					{#if remoteOnly && selectedRow.remote}<span class="st-mono">{selectedRow.remote}</span>{/if}
					{#if selectedRow.workers.length}<ProjectWorkerChips workers={selectedRow.workers} kind={selectedRow.kind} projectName={selectedRow.name}
						onSetup={(w) => setUpFromChip(selectedRow, w)} onOpen={(w) => { activeTab = 'workers'; focusInstallId = w.install_id; }} />{/if}
				{/snippet}
				{#snippet primary()}
					{#if activeTab === 'mcp'}<button class="st-btn" onclick={() => mcpTabRef?.openAddForm()}><Plus />Add server</button>{/if}
				{/snippet}
				{#snippet banner()}
					{#if error}<InlineBanner>{error}{#snippet actions()}<button class="st-btn sm" onclick={() => (error = null)}>Dismiss</button>{/snippet}</InlineBanner>{/if}
					{#if selectedRow.localId && !liveRow}<InlineBanner>This project was deleted elsewhere. Any unsaved fields remain available below until you leave.</InlineBanner>{/if}
					{#if remoteOnly}<InlineBanner tone="info">{selectedRow.name} isn't on {selfName}. Set it up here from Workers to change its settings, MCP servers, agents, hooks and memory.</InlineBanner>{/if}
				{/snippet}
				{#key selectedRow.key}
				{#if activeTab === 'workers' && showWorkersTab && selectedRow.projectV2Id}
					<ProjectWorkersTab projectRef={{ project_v2_id: selectedRow.projectV2Id }} projectName={selectedRow.name} localProjectId={selectedRow.localId} {focusInstallId} onChanged={() => void loadProjectsOverview()} />
				{:else if selectedProject || (selectedRow.localId && activeTab === 'general')}
					{#if activeTab === 'general'}<ProjectGeneralTab projectId={selectedRow.localId!} />
					{:else if activeTab === 'mcp'}<ProjectMcpTab bind:this={mcpTabRef} projectId={selectedRow.localId!} />
					{:else if activeTab === 'agents'}<ProjectAgentsTab projectId={selectedRow.localId!} />
					{:else if activeTab === 'hooks'}<ProjectHooksTab projectId={selectedRow.localId!} />
					{:else if activeTab === 'memory'}<ProjectMemoryTab projectId={selectedRow.localId!} />{/if}
				{/if}
				{/key}
			</DetailPane>
		{:else}
			<ViewMain width="reading"><EmptyDetail title="Choose a project" message="Select a project to manage its settings and workers.">
				<button class="st-btn primary" onclick={() => (showAddModal = true)}><Plus />New project</button>
			</EmptyDetail></ViewMain>
		{/if}
	{/snippet}
</ViewShell>
<AddProjectModal open={showAddModal} onClose={() => (showAddModal = false)} onCreated={() => void loadProjectsOverview()} />
