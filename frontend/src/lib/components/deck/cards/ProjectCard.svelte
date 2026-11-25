<script lang="ts">
	/** Projects uses the shared master/detail view; project and worker actions stay local to this page. */
	import { onDestroy, onMount, untrack } from 'svelte';
	import { ViewShell, ViewPanel, ViewMain, PanelSearch, PanelIconButton, DetailPane, EmptyDetail, ListItem, Segmented, InlineBanner } from '$lib/components/layout/view';
	import { platform } from '$lib/stores/platform.svelte';
	import { appData as tabs, projects } from '$lib/stores/app-data.svelte';
	import { loadProjectsOverview, projectsOverview, projectsOverviewSupported } from '$lib/stores/projects-overview';
	import { Plus, FolderOpen, FolderGit2, Settings, GitBranch } from 'lucide-svelte';
	import ProjectWorkersTab from './ProjectWorkersTab.svelte';
	import ProjectWorkerChips from './ProjectWorkerChips.svelte';
	import ProjectResourcesTab, { type ResourceId } from './ProjectResourcesTab.svelte';
	import ProjectSettingsDrawer from './ProjectSettingsDrawer.svelte';
	import ProjectRepositoryDialog from './ProjectRepositoryDialog.svelte';
	import ProjectGroupChip from './ProjectGroupChip.svelte';
	import ProjectNewChat from './ProjectNewChat.svelte';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ProjectKind, ProjectOverviewRow, ProjectRef, ProjectWorker, ProjectWorkersResponse } from '$lib/api/project-workers';
	import { SETUP_POLL_MS } from '$lib/stores/project-setup';
	import { hasProject, heldByNoWorker, isOnThisWorker, isRunning, missingCount, selfWorker, filterGeometry, workerFilterSlot, startFilterDeadline, type FilterGeometry } from './project-workers-view';
	import { refreshWorkerPresence, workerInstalls } from '$lib/stores/worker-presence';
	import { workerDisplayName } from '$lib/components/ui/worker-name';
	import './project-tabs.css';

	let stacked = $state(false);
	let mounted = $state(false);
	onMount(() => { mounted = true; });
	/** The selected row's key: its logical id when known, else this worker's id. */
	let selectedKey = $state<string | null>(null);

	// The detail's two views: the worker table and this worker's resources.
	type DetailTab = 'workers' | 'resources';
	let activeTab = $state<DetailTab>('workers');
	/** The worker whose inspector is open (a row or a header chip chose it). */
	let selectedInstallId = $state<string | null>(null);
	/** The resource open in Resources (null on a phone: the list of four). */
	let resourceSection = $state<ResourceId | null>(null);
	let showSettings = $state(false);
	let showRepoDialog = $state(false);

	// Add project modal state
	let showAddModal = $state(false);

	// Search + worker filter
	let searchQuery = $state('');
	let workerFilter = $state<'all' | 'here'>('all');

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
	// Each visit decides the worker filter row's space afresh (filterGeometry): this visit's
	// answers, failures and deadline only, never a previous visit's.
	let overviewSettled = $state(false);
	let registryFailed = $state(false);
	let deadlinePassed = $state(false);
	let filterGeometryDecided = $state<FilterGeometry | null>(null);
	let visit = 0;
	$effect(() => {
		if (!shown) return;
		const id = ++visit;
		overviewSettled = false; registryFailed = false; deadlinePassed = false; filterGeometryDecided = null;
		void loadProjectsOverview().finally(() => { if (id === visit) overviewSettled = true; });
		// The registry decides the filter row's space when the overview is slower (filterGeometry).
		if (untrack(() => $workerInstalls) === null) refreshWorkerPresence().catch(() => { if (id === visit) registryFailed = true; });
		const cancelDeadline = startFilterDeadline(() => { if (id === visit) deadlinePassed = true; });
		return () => {
			cancelDeadline(); visit++;
			overviewSettled = false; registryFailed = false; deadlinePassed = false; filterGeometryDecided = null;
		};
	});

	const overviewOn = $derived($projectsOverviewSupported === true);
	const selfName = $derived.by(() => {
		for (const row of $projectsOverview) {
			const self = selfWorker(row.workers);
			if (self) return workerDisplayName(self.device_name, 'Local');
		}
		// Before the overview answers, the registry names this worker the same way.
		return workerDisplayName($workerInstalls?.find((i) => i.is_self)?.device_name, 'Local');
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
	// The first decision of the visit stands: later answers fill or blank the row, never add or remove it.
	$effect.pre(() => {
		if (!shown || filterGeometryDecided !== null) return;
		filterGeometryDecided = filterGeometry({ supported: $projectsOverviewSupported, overviewWorkerCount: workerCount,
			installs: $workerInstalls, registryFailed, deadlinePassed });
	});
	const filterSlot = $derived(workerFilterSlot(filterGeometryDecided, $projectsOverviewSupported, workerCount));

	// Filtered projects
	const filteredRows = $derived.by(() => {
		let rows = allRows;
		// Only a filter the user can see narrows the list.
		if (filterSlot === 'shown' && workerFilter === 'here') rows = rows.filter((r) => r.onHere);
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

	/** Until the list's shape is decided it paints nothing that could move: no rows before the filter
	 *  row's space is decided, and no empty state while peers may still bring projects. Both end at
	 *  the visit's deadline, so stalled requests cannot hold the list back for long. */
	const listPending = $derived(filterSlot === 'unknown' || (filterSlot === 'reserved' && $projectsOverviewSupported === null
		&& !overviewSettled && !deadlinePassed && !filteredRows.length && !searchQuery.trim()));

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
	/**
	 * The worker table's project: its logical id, or (a project of this worker
	 * the overview has not listed) its local id, so its folder still changes in
	 * the one safe editor. When the Workers API has no answer for that local id
	 * (an older worker), or answers for another checkout of the same origin,
	 * Project settings changes the folder instead.
	 */
	const workersRef = $derived.by((): ProjectRef | null => {
		if (!overviewOn || !selectedRow) return null;
		if (selectedRow.projectV2Id) return { project_v2_id: selectedRow.projectV2Id };
		return selectedRow.localId ? { project_id: selectedRow.localId } : null;
	});
	let noWorkersFor = $state<string | null>(null);
	const showWorkersTab = $derived(!!workersRef && noWorkersFor !== selectedRow?.key);
	const detailMissing = $derived(selectedRow ? missingCount(selectedRow.workers) : 0);

	/** This worker's row as the worker table last read it (rows the overview has not listed have no workers). */
	let tableSelf = $state<{ key: string; installId: string; has: boolean } | null>(null);
	function tableLoaded(key: string, res: ProjectWorkersResponse) {
		const self = res.workers.find((w) => w.is_self);
		tableSelf = self ? { key, installId: self.install_id, has: hasProject(self.state) } : null;
	}
	const overviewSelf = $derived(selectedRow ? selfWorker(selectedRow.workers) : null);
	const selfInstallId = $derived(overviewSelf?.install_id ?? (tableSelf && tableSelf.key === selectedRow?.key ? tableSelf.installId : null));
	/** This worker's inspector can remove its copy (it holds the project, and the page has a worker table). */
	const selfRowRemoves = $derived(showWorkersTab && !!selectedRow?.localId && !!selfInstallId
		&& (overviewSelf ? hasProject(overviewSelf.state) : !!tableSelf?.has));
	let askSelfRemoval = $state(false);
	const orphaned = $derived(!!selectedRow && heldByNoWorker(selectedRow.workers));
	const isGit = $derived(selectedRow?.kind === 'git' || (!!selectedRow?.remote && selectedRow.kind !== 'folder'));

	// Without the worker table (no overview for this row, or not loaded yet) the
	// detail shows its resources; the chosen tab comes back once the table can show.
	const shownTab = $derived<DetailTab>(showWorkersTab ? activeTab : 'resources');

	function handleRowClick(row: ListRow, tab?: DetailTab, installId: string | null = null) {
		selectedKey = row.key;
		activeTab = tab ?? 'workers';
		selectedInstallId = installId;
		resourceSection = null;
		showSettings = false;
		showRepoDialog = false;
		askSelfRemoval = false;
	}

	function handleBack() {
		selectedKey = null;
		selectedInstallId = null;
		resourceSection = null;
		showSettings = false;
		showRepoDialog = false;
	}

	/** Open this worker's row in the table: where its folder changes and a setup starts. */
	function openSelf() {
		activeTab = 'workers';
		selectedInstallId = selfInstallId;
	}

	/** Settings' "Remove from <this worker>": its inspector opens with the removal asking. */
	function removeSelf() {
		openSelf();
		askSelfRemoval = true;
	}

	function projectGone() {
		showSettings = false;
		handleBack();
		void loadProjectsOverview();
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
	const detailTabs = $derived(showWorkersTab ? [
		{ id: 'workers', label: 'Workers', count: detailMissing > 0 ? `${detailMissing} missing` : undefined },
		{ id: 'resources', label: 'Resources', title: remoteOnly ? `${selectedRow?.name} isn't on ${selfName}. Set it up here to edit its resources.` : undefined }
	] : undefined);

</script>

<ViewShell label="Projects" detailOpen={!!selectedRow} detailKey={selectedKey} onBack={handleBack} bind:stacked>
	{#snippet panel()}
		<ViewPanel title="Projects" count={allRows.length} loading={listPending}>
			{#snippet actions()}<PanelIconButton label="New project" icon={Plus} onclick={() => (showAddModal = true)} />{/snippet}
			{#snippet search()}<PanelSearch bind:value={searchQuery} label="Search projects" placeholder="Search projects" />{/snippet}
			{#snippet filters()}
				{#if filterSlot === 'shown' || filterSlot === 'reserved'}
					<div class="worker-filter" class:reserved={filterSlot === 'reserved'} aria-hidden={filterSlot === 'reserved' || undefined} inert={filterSlot === 'reserved'}>
						<Segmented label="Show projects" size="sm" bind:value={workerFilter} options={[{ value: 'all', label: 'All workers' }, { value: 'here', label: `On ${selfName}`, title: `Show only projects on ${selfName}` }]} />
					</div>
				{/if}
			{/snippet}
			<div data-ui-projects={overviewOn ? 'overview' : 'local'}>
				{#if !listPending}
				{#each filteredRows as row (row.key)}
					<div data-project-key={row.key}>
						<ListItem id={row.key} name={row.name} summary={rowMeta(row)} icon={row.kind === 'git' ? FolderGit2 : FolderOpen}
							status={row.onHere ? 'ok' : 'off'} selected={selectedRow?.key === row.key}
							ariaLabel={`Open ${row.name}${row.onHere ? '' : ` (not on ${selfName})`}`} onselect={() => handleRowClick(row)} />
					</div>
				{:else}
					<EmptyDetail title={searchQuery ? `No projects match "${searchQuery}"` : workerFilter === 'here' && allRows.length ? `No projects on ${selfName} yet` : 'No projects configured yet'} message="Projects keep your work and settings together."
						actionLabel={searchQuery ? 'Clear search' : workerFilter === 'here' && allRows.length ? 'Show all workers' : undefined}
						onAction={() => { searchQuery = ''; workerFilter = 'all'; }} icon={FolderOpen}>
						<button class="st-btn primary" onclick={() => (showAddModal = true)}><Plus />New project</button>
					</EmptyDetail>
				{/each}
				{/if}
			</div>
		</ViewPanel>
	{/snippet}
	{#snippet main(back)}
		{#if selectedRow}
			{@const row = selectedRow}
			{@const ref = overviewOn && row.projectV2Id ? { project_v2_id: row.projectV2Id } : null}
			{#snippet settingsButton()}
				<button type="button" class="st-btn icon" aria-label="Project settings" title="Project settings" aria-haspopup="dialog" onclick={() => (showSettings = true)}><Settings /></button>
			{/snippet}
			<DetailPane width="data" name={row.name} icon={row.kind === 'git' ? FolderGit2 : FolderOpen} onBack={back} backLabel="Projects"
				tabs={detailTabs} tab={shownTab} onTabChange={(tab) => { activeTab = tab as DetailTab; }} phonePrimary="inline" phoneMenu={settingsButton}>
				{#snippet subtitle()}
					<span class="project-idline" data-ui-project-identity>
						{#if isGit}
							<!-- Every Git project offers the change, a missing remote too (it takes a URL). -->
							{@const repoLabel = row.remote || 'Repository not reported'}
							{#if $isAdmin && ref}
								<button type="button" class="project-chip" aria-label={row.remote ? `Repository ${row.remote}. Change it on all workers` : 'Repository not reported. Set it on all workers'} title="Change the repository on all workers" onclick={() => (showRepoDialog = true)}>
									<GitBranch aria-hidden="true" /><span class="project-chip-value" class:st-mono={!!row.remote}>{repoLabel}</span>
								</button>
							{:else}<span class="project-chip static"><GitBranch aria-hidden="true" /><span class="project-chip-value" class:st-mono={!!row.remote}>{repoLabel}</span></span>{/if}
						{/if}
						{#if selectedProject}<ProjectGroupChip projectId={selectedProject.id} projectName={row.name} />{/if}
						{#if row.workers.length}<ProjectWorkerChips workers={row.workers} kind={row.kind} projectName={row.name}
							onOpen={(w) => { activeTab = 'workers'; selectedInstallId = w.install_id; }} />{/if}
					</span>
				{/snippet}
				{#snippet primary()}
					{#if !stacked}{@render settingsButton()}{/if}
					<ProjectNewChat workers={ref ? row.workers : null} kind={row.kind} localProjectId={row.localId} projectV2Id={row.projectV2Id} />
				{/snippet}
				{#snippet banner()}
					{#if row.localId && !liveRow}<InlineBanner>This project was deleted elsewhere. Any unsaved fields remain available in Project settings until you leave.</InlineBanner>{/if}
				{/snippet}
				{#key row.key}
				{#if shownTab === 'workers' && workersRef}
					{@const key = row.key}
					<ProjectWorkersTab projectRef={workersRef} projectName={row.name} localProjectId={row.localId} {stacked} bind:selectedInstallId
						onChanged={() => void loadProjectsOverview()} onLoaded={(res) => tableLoaded(key, res)}
						onUnavailable={row.projectV2Id ? undefined : () => { noWorkersFor = key; }} expectLocalId={row.projectV2Id ? null : row.localId}
						{askSelfRemoval} onSelfRemovalAsked={() => { askSelfRemoval = false; }} />
				{:else}
					<ProjectResourcesTab projectId={selectedProject?.id ?? null} projectName={row.name} {selfName} {stacked} bind:section={resourceSection} onSetUpHere={showWorkersTab && selfInstallId ? openSelf : undefined} />
				{/if}
				{/key}
			</DetailPane>
			{#if showSettings}
				<ProjectSettingsDrawer projectName={row.name} localProjectId={selectedProject?.id ?? null} projectRef={ref} kind={row.kind} {selfName} {orphaned}
					workerTable={showWorkersTab} onOpenSelf={showWorkersTab && selfInstallId ? openSelf : undefined}
					onRemoveSelf={selfRowRemoves ? removeSelf : undefined} onRemoved={projectGone} onClose={() => (showSettings = false)} />
			{/if}
			{#if showRepoDialog && ref}
				<ProjectRepositoryDialog projectRef={ref} remote={row.remote} onClose={() => (showRepoDialog = false)} onChanged={() => void loadProjectsOverview()} />
			{/if}
		{:else}
			<ViewMain width="reading"><EmptyDetail icon={FolderOpen} title={allRows.length ? 'Choose a project' : 'No projects configured yet'} message={allRows.length ? 'Select a project to manage its settings and workers.' : 'Projects keep your work and settings together.'}>
				<button class="st-btn primary" onclick={() => (showAddModal = true)}><Plus />New project</button>
			</EmptyDetail></ViewMain>
		{/if}
	{/snippet}
</ViewShell>
<AddProjectModal open={showAddModal} onClose={() => (showAddModal = false)} onCreated={() => void loadProjectsOverview()} />

<style>
	/* The filter's space, held while the overview may still show it (filterGeometry). */
	.worker-filter { display: flex; align-items: center; min-width: 0; max-width: 100%; }
	.worker-filter.reserved { visibility: hidden; }
	/* One line whatever the worker's name: the held row and the filled one keep the same height. A
	   long name is shortened with an ellipsis; the button's name and tooltip keep it whole. */
	.worker-filter :global(.st-seg) { flex-wrap: nowrap; min-width: 0; }
	.worker-filter :global(.st-seg button) { min-width: 0; }
	.worker-filter :global(.st-seg button:first-child) { flex-shrink: 0; }
	.worker-filter :global(.st-seg-label) { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	/* While the list's shape is unknown (listPending) its region holds nothing; it is not painted
	   until then, so its first paint already sits under the filter row's final space. */
	:global(.view-panel[aria-label="Projects"][aria-busy="true"] > .panel-body) { visibility: hidden; }
	/* ViewPanel folds the filter row and the search field's gap away beside an empty state; once this
	   visit holds the row's space it keeps it, so "No projects" giving way to a peer's projects moves
	   nothing. */
	:global(.view-panel.view-panel[aria-label="Projects"]:has(.worker-filter) > .panel-filters.panel-filters) { display: flex; }
	:global(.view-panel.view-panel[aria-label="Projects"]:has(.worker-filter) > .panel-search.panel-search) { padding-bottom: 8px; }
</style>
