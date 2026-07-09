<script lang="ts">
	/**
	 * SessionsBoard - Mission-control navigator (scales to many projects/sessions)
	 *
	 * Two levels:
	 *  - Overview: a "Recent" strip + a grid of project cards (one per project,
	 *    with session count / running / last-active). Avoids rendering thousands
	 *    of session cards at once.
	 *  - Project: a dense, searchable session list (rows) for the chosen project.
	 * A global search box flips the overview into a flat results list.
	 *
	 * Built on the app's theme tokens.
	 */
	import { Plus, X, Star, FolderClosed, MessageSquare, ChevronRight, ChevronDown, GitBranch, GitFork, Play, Monitor, Link2, Unlink, Check, CalendarClock } from 'lucide-svelte';
	import BrushCleaning from 'lucide-svelte/icons/brush-cleaning';
	import { browser } from '$app/environment';
	import { appData, sessions, projects } from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { toggleSessionFavorite, forkSession, adoptSession, api } from '$lib/api/client';
	import type { Session } from '$lib/api/client';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { toast } from '$lib/stores/toast';

	interface Props {
		onOpenSession: (sessionId: string) => void;
		onNewSession: () => void;
		onNewSessionForProject?: (projectId: string) => void;
	}

	// onNewSessionForProject is part of the component contract but the board no
	// longer renders its own "New session" button (the top bar provides it), so
	// it is intentionally left undestructured.
	let { onOpenSession, onNewSession }: Props = $props();

	const UNGROUPED = '__ungrouped__';
	const RECENT_LIMIT = 10;

	// --- Collapsible board sections ---
	// Users with many sessions can fold away the Pinned / Recent / Projects
	// sections they don't need. State persists across reloads (per browser).
	const COLLAPSE_KEY = 'ai-shuffle-board-collapsed-sections';
	function loadCollapsed(): string[] {
		if (!browser) return [];
		try {
			const raw = localStorage.getItem(COLLAPSE_KEY);
			const parsed = raw ? JSON.parse(raw) : [];
			return Array.isArray(parsed) ? parsed : [];
		} catch {
			return [];
		}
	}
	let collapsedSections = $state<Set<string>>(new Set(loadCollapsed()));
	function isCollapsed(key: string): boolean {
		return collapsedSections.has(key);
	}
	function toggleSection(key: string) {
		const next = new Set(collapsedSections);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		collapsedSections = next;
		if (browser) {
			try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...next])); } catch { /* ignore */ }
		}
	}

	let selectedKey = $state<string | null>(null);
	let deletingSessionId = $state<string | null>(null);
	let deleteLoading = $state(false);

	const activeId = $derived($activeSession.sessionId);
	const projectsById = $derived(new Map($projects.map((p) => [p.id, p])));

	// --- Cross-install device filter ---
	// null = all devices. Only surfaced when sessions from more than one device
	// are present, so single-install users never see it.
	let deviceFilter = $state<string | null>(null);

	interface DeviceOpt { id: string; name: string; online: boolean }

	// Distinct devices represented across the loaded sessions. A NULL/own
	// instance_id is bucketed as "This device".
	const deviceOptions = $derived.by((): DeviceOpt[] => {
		const map = new Map<string, DeviceOpt>();
		for (const s of $sessions) {
			const id = s.is_remote && s.instance_id ? s.instance_id : '__local__';
			if (map.has(id)) {
				if (s.device_online) map.get(id)!.online = true;
				continue;
			}
			map.set(id, {
				id,
				name: id === '__local__' ? 'This device' : s.device_name || 'Unknown device',
				online: id === '__local__' ? true : !!s.device_online
			});
		}
		return [...map.values()].sort((a, b) => a.name.localeCompare(b.name));
	});

	function sessionDeviceId(s: Session): string {
		return s.is_remote && s.instance_id ? s.instance_id : '__local__';
	}

	// Sessions after applying the device filter — every list below derives from
	// this so the filter cuts across pinned/recent/projects uniformly.
	const visibleSessions = $derived(
		deviceFilter ? $sessions.filter((s) => sessionDeviceId(s) === deviceFilter) : $sessions
	);

	// Drop a stale filter if its device disappears (e.g. its last session deleted)
	// so the board never shows an empty list with no visible way to reset.
	$effect(() => {
		if (deviceFilter && !deviceOptions.some((d) => d.id === deviceFilter)) {
			deviceFilter = null;
		}
	});

	function isStreaming(id: string): boolean {
		if ($activeSession.sessionId === id) return $activeSession.isStreaming;
		return $streamingSessions.has(id);
	}
	function needsAttention(id: string): boolean {
		return $backgroundPermissions.has(id);
	}
	// Aggregate "needs you" signal so a user juggling many sessions never has to
	// click through each one to find the one parked on a question.
	const awaitingCount = $derived($backgroundPermissions.size);
	function openFirstAwaiting(): void {
		const first = $backgroundPermissions.keys().next().value as string | undefined;
		if (first) onOpenSession(first);
	}
	function tsOf(s: Session): number {
		return new Date(s.updated_at || s.created_at).getTime();
	}
	function sortSessions(list: Session[]): Session[] {
		return [...list].sort((a, b) => {
			if (!!a.is_favorite !== !!b.is_favorite) return a.is_favorite ? -1 : 1;
			return tsOf(b) - tsOf(a);
		});
	}
	function projectName(s: Session): string {
		// Prefer the server-resolved name: $projects is instance-filtered, so a
		// cross-install session's project isn't in projectsById.
		if (s.project_name) return s.project_name;
		return s.project_id ? projectsById.get(s.project_id)?.name || 'Ungrouped' : 'Ungrouped';
	}

	const totalCount = $derived(visibleSessions.length);

	interface Group {
		key: string;
		name: string;
		projectId: string | null;
		sessions: Session[];
		lastActivity: number;
		running: number;
		latest: Session | null;
		devices: DeviceOpt[]; // Distinct devices this (possibly linked) project spans
	}

	// Distinct devices represented by a list of sessions — same bucketing as
	// deviceOptions, used to label which device(s) a project card belongs to.
	function devicesOf(list: Session[]): DeviceOpt[] {
		const map = new Map<string, DeviceOpt>();
		for (const s of list) {
			const id = sessionDeviceId(s);
			if (map.has(id)) {
				if (s.device_online) map.get(id)!.online = true;
				continue;
			}
			map.set(id, {
				id,
				name: id === '__local__' ? 'This device' : s.device_name || 'Unknown device',
				online: id === '__local__' ? true : !!s.device_online
			});
		}
		return [...map.values()].sort((a, b) => a.name.localeCompare(b.name));
	}

	const projectGroups = $derived.by((): Group[] => {
		const groups = new Map<string, Session[]>();
		for (const s of visibleSessions) {
			// Fold same-repo clones from every install into one logical project
			// via the server-resolved link group; fall back to the raw project id.
			const key = s.link_group_id || s.project_id || UNGROUPED;
			if (!groups.has(key)) groups.set(key, []);
			groups.get(key)!.push(s);
		}
		const arr: Group[] = [...groups.entries()].map(([key, list]) => {
			const sorted = sortSessions(list);
			// A concrete project id from this group, for project-scoped actions
			// (a link group folds several, so just take a representative one).
			const projectId = key === UNGROUPED ? null : (list.find((s) => s.project_id)?.project_id ?? null);
			// Resolve the display name: prefer a locally-known project, then the
			// server-provided project_name (set for cross-install projects too).
			const name = key === UNGROUPED
				? 'Ungrouped'
				: (projectId && projectsById.get(projectId)?.name)
					|| list.find((s) => s.project_name)?.project_name
					|| projectsById.get(key)?.name
					|| 'Ungrouped';
			return {
				key,
				name,
				projectId,
				sessions: list,
				lastActivity: list.reduce((m, s) => Math.max(m, tsOf(s)), 0),
				running: list.filter((s) => isStreaming(s.id)).length,
				latest: sorted[0] ?? null,
				devices: devicesOf(list)
			};
		});
		arr.sort((a, b) => b.lastActivity - a.lastActivity);
		return arr;
	});

	// Pinned (favorited) sessions get their own section so they always stay
	// visible at the top instead of competing with everything else for the
	// limited Recent slots. Recent therefore excludes favorites.
	const pinned = $derived(visibleSessions.filter((s) => s.is_favorite).sort((a, b) => tsOf(b) - tsOf(a)));
	const recent = $derived(
		visibleSessions.filter((s) => !s.is_favorite).sort((a, b) => tsOf(b) - tsOf(a)).slice(0, RECENT_LIMIT)
	);

	const selectedGroup = $derived(selectedKey ? projectGroups.find((g) => g.key === selectedKey) ?? null : null);

	type Mode = 'overview' | 'project';
	const mode = $derived<Mode>(selectedGroup ? 'project' : 'overview');

	const displayList = $derived.by((): Session[] =>
		selectedGroup ? sortSessions(selectedGroup.sessions) : []
	);

	function openProject(key: string) {
		selectedKey = key;
	}
	function backToOverview() {
		selectedKey = null;
	}

	// --- Manual project linking (admin) ---
	// A representative *local* project for the open group ($projects is
	// instance-filtered, so only this install's rows appear). We manage links on
	// the local row; the lock flag lives in its settings.
	const selectedLocalProject = $derived.by(() => {
		if (!selectedGroup) return null;
		for (const s of selectedGroup.sessions) {
			if (s.project_id && projectsById.has(s.project_id)) return projectsById.get(s.project_id)!;
		}
		return null;
	});
	const linkLocked = $derived(
		!!(selectedLocalProject?.settings as Record<string, unknown> | undefined)?.link_group_locked
	);
	// Other groups whose representative project is local here — valid manual-link targets.
	const linkTargets = $derived(
		selectedGroup
			? projectGroups.filter(
					(g) => g.key !== selectedGroup.key && g.projectId && projectsById.has(g.projectId)
				)
			: []
	);
	let linkBusy = $state(false);

	async function linkTo(targetProjectId: string) {
		if (!selectedLocalProject || !targetProjectId || linkBusy) return;
		linkBusy = true;
		try {
			await api.post(`/projects/${selectedLocalProject.id}/link`, { target_project_id: targetProjectId });
			await appData.loadProjects();
			await appData.loadSessions();
			toast.push('Projects linked', { tone: 'success' });
		} catch {
			toast.push('Failed to link projects', { tone: 'error' });
		} finally {
			linkBusy = false;
		}
	}

	async function unlink() {
		if (!selectedLocalProject || linkBusy) return;
		linkBusy = true;
		try {
			await api.delete(`/projects/${selectedLocalProject.id}/link`);
			await appData.loadProjects();
			await appData.loadSessions();
			toast.push('Project unlinked', { tone: 'success' });
		} catch {
			toast.push('Failed to unlink project', { tone: 'error' });
		} finally {
			linkBusy = false;
		}
	}

	// --- Header filter dropdowns -------------------------------------------
	// Custom dropdowns (not native <select>) so they match the app's other
	// dropdowns — themed menu, hover states, active checkmark, fade animation.
	let deviceMenuOpen = $state(false);
	let deviceMenuRef = $state<HTMLDivElement | null>(null);
	let linkMenuOpen = $state(false);
	let linkMenuRef = $state<HTMLDivElement | null>(null);

	const deviceFilterLabel = $derived(
		deviceFilter ? (deviceOptions.find((d) => d.id === deviceFilter)?.name ?? 'All devices') : 'All devices'
	);

	function selectDevice(id: string | null) {
		deviceFilter = id;
		deviceMenuOpen = false;
	}

	function chooseLink(targetProjectId: string | null) {
		linkMenuOpen = false;
		if (targetProjectId) linkTo(targetProjectId);
	}

	function handleFilterWindowClick(e: MouseEvent) {
		const t = e.target as Node;
		if (deviceMenuOpen && deviceMenuRef && !deviceMenuRef.contains(t)) deviceMenuOpen = false;
		if (linkMenuOpen && linkMenuRef && !linkMenuRef.contains(t)) linkMenuOpen = false;
	}
	function handleFilterWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			deviceMenuOpen = false;
			linkMenuOpen = false;
		}
	}

	async function toggleFavorite(s: Session) {
		try {
			await toggleSessionFavorite(s.id);
			await appData.loadSessions();
		} catch {
			toast.push('Failed to update favorite', { tone: 'error' });
		}
	}
	async function handleToggleFavorite(e: MouseEvent | KeyboardEvent, s: Session) {
		e.stopPropagation();
		await toggleFavorite(s);
	}

	// Row activation — a plain click / Enter / Space opens the session. (The
	// former right-click + long-press context menu was replaced by the inline
	// action-rail buttons on each row; see the Fork / Resume buttons below.)
	function onRowClick(s: Session) {
		openSession(s.id);
	}
	function onRowKeydown(e: KeyboardEvent, s: Session) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			openSession(s.id);
		}
	}

	// --- Row action-rail handlers ------------------------------------------
	// Each stops propagation so the row's own click (open the session) never
	// also fires when a rail button is pressed.
	async function handleResume(e: MouseEvent | KeyboardEvent, s: Session) {
		e.stopPropagation();
		await resumeHere(s);
	}
	async function handleFork(e: MouseEvent | KeyboardEvent, s: Session) {
		e.stopPropagation();
		await forkAndOpen(s);
	}
	// "Resume here" adopts a foreign-instance session to this install and opens
	// it; for a local session there is nothing to adopt, so it is a plain open
	// and the tooltip reads "Open" instead.
	function resumeTitle(s: Session): string {
		if (!s.is_remote) return 'Open';
		return s.device_name ? `Resume here (${s.device_name})` : 'Resume here';
	}

	// "Resume here": adopt a foreign-instance session to this install (a no-op
	// server-side for a local session), reload so worktree/ownership fields are
	// fresh, then open. Worktree sessions rebind via the existing recreate path.
	async function resumeHere(s: Session) {
		try {
			if (s.is_remote) {
				await adoptSession(s.id);
				await appData.loadSessions();
			}
			onOpenSession(s.id);
		} catch (e) {
			const detail = (e as { detail?: string } | null)?.detail;
			toast.push(detail || 'Could not resume this session here', { tone: 'error' });
		}
	}

	// "Fork": duplicate the whole conversation into a new session, then open it.
	async function forkAndOpen(s: Session) {
		try {
			const res = await forkSession(s.id);
			await appData.loadSessions();
			toast.push('Session forked', { tone: 'success' });
			onOpenSession(res.session_id);
		} catch {
			toast.push('Failed to fork session', { tone: 'error' });
		}
	}

	async function handleDelete(e: MouseEvent | KeyboardEvent, id: string) {
		e.stopPropagation();
		if (deletingSessionId === id) {
			deleteLoading = true;
			try {
				if (activeId === id) onNewSession();
				await appData.deleteSession(id);
			} catch {
				toast.push('Failed to delete session', { tone: 'error' });
			} finally {
				deleteLoading = false;
				deletingSessionId = null;
			}
		} else {
			deletingSessionId = id;
			setTimeout(() => { if (deletingSessionId === id) deletingSessionId = null; }, 3000);
		}
	}
	function cancelDelete(e: MouseEvent | KeyboardEvent) {
		e.stopPropagation();
		deletingSessionId = null;
	}
	// Open a session from its row — but never while that row is showing its
	// delete confirmation (or the delete is in flight). The confirm button is
	// natively `disabled` during the request, and the browser routes a click on
	// a disabled button to its parent (the row), which would otherwise open the
	// just-deleted session as an empty chat.
	function openSession(id: string) {
		if (deletingSessionId === id) return;
		onOpenSession(id);
	}

	function relTime(input: string | number): string {
		const ms = typeof input === 'number' ? input : new Date(input).getTime();
		if (!ms) return '';
		const diff = Date.now() - ms;
		const min = Math.floor(diff / 60000);
		const hr = Math.floor(diff / 3600000);
		const day = Math.floor(diff / 86400000);
		if (min < 1) return 'now';
		if (min < 60) return `${min}m`;
		if (hr < 24) return `${hr}h`;
		if (day < 30) return `${day}d`;
		return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
	function fmtCost(c: number): string {
		return c < 0.01 ? '<$0.01' : `$${c.toFixed(2)}`;
	}
	function fmtDuration(s: Session): string {
		const diff = new Date(s.updated_at || s.created_at).getTime() - new Date(s.created_at).getTime();
		if (diff < 60000) return '<1m';
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m`;
		const hrs = Math.floor(mins / 60);
		const rem = mins % 60;
		if (hrs < 24) return rem > 0 ? `${hrs}h ${rem}m` : `${hrs}h`;
		return `${Math.floor(hrs / 24)}d`;
	}
</script>

<svelte:window onclick={handleFilterWindowClick} onkeydown={handleFilterWindowKeydown} />

<div class="board card-system">
	<!-- Header -->
	<header class="board-header">
		<div class="board-head-left">
			{#if mode === 'project' && selectedGroup}
				<button class="crumb" onclick={backToOverview}>Sessions</button>
				<ChevronRight size={14} strokeWidth={1.6} class="crumb-sep" />
				<span class="crumb-current">{selectedGroup.name}</span>
				<span class="board-count">{selectedGroup.sessions.length}</span>
			{:else}
				<h1 class="board-title">Sessions</h1>
				<span class="board-count">{totalCount}</span>
			{/if}
			{#if awaitingCount > 0}
				<button
					type="button"
					class="awaiting-pill"
					onclick={openFirstAwaiting}
					title="Jump to a session awaiting your input"
				>
					<MessageSquare size={12} strokeWidth={2} />
					{awaitingCount} awaiting input
				</button>
			{/if}
		</div>
		{#if mode === 'overview' && deviceOptions.length > 1}
			<div class="board-head-right">
				<div class="board-filter" bind:this={deviceMenuRef}>
					<button
						type="button"
						class="filter-btn"
						onclick={(e) => { e.stopPropagation(); deviceMenuOpen = !deviceMenuOpen; }}
						aria-haspopup="menu"
						aria-expanded={deviceMenuOpen}
						title="Filter sessions by device"
					>
						<Monitor size={13} strokeWidth={1.6} />
						<span class="filter-label">{deviceFilterLabel}</span>
						<ChevronDown size={12} strokeWidth={1.8} class="filter-caret {deviceMenuOpen ? 'open' : ''}" />
					</button>
					{#if deviceMenuOpen}
						<div class="filter-menu" role="menu" aria-label="Filter by device">
							<button
								class="filter-item"
								class:active={deviceFilter === null}
								role="menuitemradio"
								aria-checked={deviceFilter === null}
								onclick={() => selectDevice(null)}
							>
								<span class="filter-item-name">All devices</span>
								{#if deviceFilter === null}<Check size={14} strokeWidth={2} />{/if}
							</button>
							{#each deviceOptions as d (d.id)}
								<button
									class="filter-item"
									class:active={deviceFilter === d.id}
									role="menuitemradio"
									aria-checked={deviceFilter === d.id}
									onclick={() => selectDevice(d.id)}
								>
									<span class="filter-item-name">{d.name}</span>
									{#if !d.online}<span class="filter-tag">offline</span>{/if}
									{#if deviceFilter === d.id}<Check size={14} strokeWidth={2} />{/if}
								</button>
							{/each}
						</div>
					{/if}
				</div>
			</div>
		{:else if mode === 'project' && selectedGroup && $isAdmin && selectedLocalProject}
			<div class="board-head-right">
				{#if linkLocked}
					<button class="link-btn" onclick={unlink} disabled={linkBusy} title="Remove manual link; regroup by git remote">
						<Unlink size={13} strokeWidth={1.6} /> Unlink
					</button>
				{:else if linkTargets.length}
					<div class="board-filter" bind:this={linkMenuRef}>
						<button
							type="button"
							class="filter-btn"
							onclick={(e) => { e.stopPropagation(); if (!linkBusy) linkMenuOpen = !linkMenuOpen; }}
							disabled={linkBusy}
							aria-haspopup="menu"
							aria-expanded={linkMenuOpen}
							title="Link this project with another so their sessions share one group"
						>
							<Link2 size={13} strokeWidth={1.6} />
							<span class="filter-label">Link with…</span>
							<ChevronDown size={12} strokeWidth={1.8} class="filter-caret {linkMenuOpen ? 'open' : ''}" />
						</button>
						{#if linkMenuOpen}
							<div class="filter-menu" role="menu" aria-label="Link with project">
								{#each linkTargets as t (t.key)}
									<button class="filter-item" role="menuitem" onclick={() => chooseLink(t.projectId)}>
										<Link2 size={13} strokeWidth={1.6} />
										<span class="filter-item-name">{t.name}</span>
									</button>
								{/each}
							</div>
						{/if}
					</div>
				{/if}
			</div>
		{/if}
	</header>

	<!-- Body -->
	<div class="board-body">
		<div class="board-wrap">
			{#if mode === 'overview'}
				{#if totalCount === 0}
					<div class="board-empty">
						<MessageSquare size={40} strokeWidth={1.25} />
						<p class="board-empty-title">No sessions yet</p>
						<button class="board-new" onclick={onNewSession}>
							<Plus size={16} strokeWidth={2} /><span>Start your first session</span>
						</button>
					</div>
				{:else}
					{#if pinned.length}
						<section class="board-section">
							<button class="section-label section-toggle" onclick={() => toggleSection('pinned')} aria-expanded={!isCollapsed('pinned')}>
								<ChevronDown size={13} strokeWidth={2.2} class="section-caret {isCollapsed('pinned') ? 'collapsed' : ''}" />
								<Star size={12} strokeWidth={2} fill="currentColor" class="section-icon" />
								Pinned <span class="section-count">{pinned.length}</span>
							</button>
							{#if !isCollapsed('pinned')}
								<div class="row-list">
									{#each pinned as s (s.id)}{@render sessionRow(s, true)}{/each}
								</div>
							{/if}
						</section>
					{/if}
					{#if recent.length}
						<section class="board-section">
							<button class="section-label section-toggle" onclick={() => toggleSection('recent')} aria-expanded={!isCollapsed('recent')}>
								<ChevronDown size={13} strokeWidth={2.2} class="section-caret {isCollapsed('recent') ? 'collapsed' : ''}" />
								Recent
							</button>
							{#if !isCollapsed('recent')}
								<div class="row-list">
									{#each recent as s (s.id)}{@render sessionRow(s, true)}{/each}
								</div>
							{/if}
						</section>
					{/if}
					<section class="board-section">
						<button class="section-label section-toggle" onclick={() => toggleSection('projects')} aria-expanded={!isCollapsed('projects')}>
							<ChevronDown size={13} strokeWidth={2.2} class="section-caret {isCollapsed('projects') ? 'collapsed' : ''}" />
							Projects <span class="section-count">{projectGroups.length}</span>
						</button>
						{#if !isCollapsed('projects')}
						<div class="proj-grid">
							{#each projectGroups as g (g.key)}
								<button class="proj-card" onclick={() => openProject(g.key)}>
									<div class="proj-top">
										<span class="proj-icon"><FolderClosed size={16} strokeWidth={1.6} /></span>
										<span class="proj-name">{g.name}</span>
										{#if g.running > 0}
											<span class="proj-running"><span class="dot dot-streaming"></span>{g.running}</span>
										{/if}
									</div>
									<div class="proj-latest">{g.latest?.title || 'Untitled'}</div>
									<div class="proj-stats">
										<span class="proj-count-pill">{g.sessions.length}</span>
										<span>{g.sessions.length === 1 ? 'session' : 'sessions'}</span>
										<span class="sep">·</span>
										<span>{relTime(g.lastActivity)}</span>
										{#if deviceOptions.length > 1 && g.devices.length}
											<span class="sep">·</span>
											{#if g.devices.length > 1}
												<span class="proj-device" title={'Spans: ' + g.devices.map((d) => d.name).join(', ')}>
													<Monitor size={11} strokeWidth={1.6} />
													{g.devices.length} devices
												</span>
											{:else}
												<span
													class="proj-device"
													class:offline={!g.devices[0].online}
													title={g.devices[0].online ? `On ${g.devices[0].name}` : `On ${g.devices[0].name} (offline)`}
												>
													<Monitor size={11} strokeWidth={1.6} />
													<span class="proj-device-name">{g.devices[0].name}</span>
												</span>
											{/if}
										{/if}
										<ChevronRight size={14} strokeWidth={1.6} class="proj-chevron" />
									</div>
								</button>
							{/each}
						</div>
						{/if}
					</section>
				{/if}
			{:else}
				<!-- project: dense session list -->
				{#if displayList.length === 0}
					<div class="board-empty">
						<MessageSquare size={36} strokeWidth={1.25} />
						<p class="board-empty-title">No sessions in this project</p>
					</div>
				{:else}
					<div class="row-list">
						{#each displayList as s (s.id)}{@render sessionRow(s, false)}{/each}
					</div>
				{/if}
			{/if}
		</div>
	</div>
</div>

{#snippet sessionRow(session: Session, showProject: boolean)}
	<div
		class="row"
		class:active={activeId === session.id}
		class:deleting={deletingSessionId === session.id}
		role="button"
		tabindex="0"
		onclick={() => onRowClick(session)}
		onkeydown={(e) => onRowKeydown(e, session)}
	>
		<span class="row-status">
			{#if needsAttention(session.id)}
				<!-- Awaiting input takes precedence over "running": a session parked
				     on a question is still technically streaming, but what the user
				     needs to see is that it's waiting on them. -->
				<span class="dot dot-attention" title="Awaiting your input"></span>
			{:else if isStreaming(session.id)}
				<span class="dot dot-streaming" title="Running"></span>
			{:else}
				<span class="dot dot-idle"></span>
			{/if}
		</span>
		<div class="row-main">
			<div class="row-line1">
				<span class="row-title">{session.title || 'Untitled'}</span>
			</div>
			<div class="row-line2">
				{#if session.automation_id}
					<span class="row-automation" title="Created by an automation (runs headless — open for history)">
						<CalendarClock size={11} strokeWidth={1.6} />
						Automation
					</span>
				{/if}
				{#if session.is_remote && session.device_name}
					<span
						class="row-device"
						class:offline={!session.device_online}
						title={session.device_online
							? (session.controllable ? `On ${session.device_name} (online)` : `On ${session.device_name} (online, view-only)`)
							: `On ${session.device_name} (offline — history only)`}
					>
						<Monitor size={11} strokeWidth={1.6} />
						<span class="row-device-name">{session.device_name}</span>
						<span class="device-dot" class:online={session.device_online}></span>
					</span>
				{:else if deviceOptions.length > 1}
					<!-- Multi-device board: label local rows too so a linked group's
					     sessions are clearly attributed to "this device". -->
					<span class="row-device" title={`On ${session.device_name || 'this device'}`}>
						<Monitor size={11} strokeWidth={1.6} />
						<span class="row-device-name">{session.device_name || 'This device'}</span>
					</span>
				{/if}
				{#if showProject}
					<span class="row-proj">{projectName(session)}</span>
				{/if}
				{#if session.total_cost_usd > 0}
					<span class="row-stat">{fmtCost(session.total_cost_usd)}</span>
					{#if session.turn_count > 0}
						<span class="sep">·</span>
						<span class="row-stat">{session.turn_count} {session.turn_count === 1 ? 'turn' : 'turns'}</span>
					{/if}
					<span class="sep">·</span>
					<span class="row-stat">{fmtDuration(session)}</span>
				{:else}
					<span class="row-stat muted">No activity yet</span>
				{/if}
				{#if session.worktree_branch}
					<span class="row-branch"><GitBranch size={11} strokeWidth={1.6} />{session.worktree_branch}</span>
				{/if}
			</div>
		</div>
		<span class="row-time">{relTime(session.updated_at || session.created_at)}</span>
		<span class="row-actions">
			<button
				class="row-action"
				title={resumeTitle(session)}
				onclick={(e) => handleResume(e, session)}
			>
				<Play size={13} strokeWidth={1.6} />
			</button>
			<button
				class="row-action"
				title="Fork session"
				onclick={(e) => handleFork(e, session)}
			>
				<GitFork size={13} strokeWidth={1.6} />
			</button>
			<button
				class="row-action fav"
				class:is-fav={session.is_favorite}
				title={session.is_favorite ? 'Unfavorite' : 'Favorite'}
				onclick={(e) => handleToggleFavorite(e, session)}
			>
				<Star size={13} strokeWidth={1.6} fill={session.is_favorite ? 'currentColor' : 'none'} />
			</button>
			{#if deletingSessionId === session.id}
				<button class="row-action danger" disabled={deleteLoading} title="Confirm clear" onclick={(e) => handleDelete(e, session.id)}>
					<BrushCleaning size={13} strokeWidth={2} />
				</button>
				<button class="row-action" title="Cancel" onclick={cancelDelete}>
					<X size={13} strokeWidth={2} />
				</button>
			{:else}
				<button class="row-action clear" title="Clear session" onclick={(e) => handleDelete(e, session.id)}>
					<BrushCleaning size={13} strokeWidth={1.6} />
				</button>
			{/if}
		</span>
	</div>
{/snippet}

<style>
	.board {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--background);
		overflow: hidden;
	}

	/* Header — title + count centered */
	.board-header {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 16px;
		padding: 16px 24px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		flex-shrink: 0;
	}
	.board-head-left { display: flex; align-items: center; justify-content: center; gap: 8px; min-width: 0; }
	.board-head-right { margin-left: auto; display: flex; align-items: center; flex-shrink: 0; }
	/* Custom dropdown — matches the app's other dropdowns (ProfileSelector /
	   BranchSelector): pill trigger + themed menu, instead of a native <select>. */
	.board-filter { position: relative; flex-shrink: 0; }
	.filter-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 30px;
		padding: 0 10px;
		background: color-mix(in oklch, var(--foreground) 6%, var(--background));
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		color: var(--muted-foreground);
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		white-space: nowrap;
		transition: border-color 120ms ease, color 120ms ease;
	}
	.filter-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--foreground); }
	.filter-btn:disabled { opacity: 0.6; cursor: default; }
	.filter-btn :global(svg) { flex-shrink: 0; }
	.filter-label { max-width: 160px; overflow: hidden; text-overflow: ellipsis; }
	.filter-btn :global(.filter-caret) { opacity: 0.6; transition: transform 150ms ease; }
	.filter-btn :global(.filter-caret.open) { transform: rotate(180deg); }

	.filter-menu {
		position: absolute;
		top: calc(100% + 4px);
		right: 0;
		min-width: 200px;
		max-width: min(280px, calc(100vw - 24px));
		max-height: 60vh;
		overflow-y: auto;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		padding: 6px;
		z-index: 50;
		animation: filter-menu-in 0.15s ease;
	}

	@keyframes filter-menu-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.filter-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 4px);
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		text-align: left;
		cursor: pointer;
		transition: background-color 120ms ease;
	}
	.filter-item:hover { background: var(--hover-overlay); }
	.filter-item.active { color: var(--primary); }
	.filter-item :global(svg) { flex-shrink: 0; color: var(--muted-foreground); }
	.filter-item.active :global(svg) { color: var(--primary); }
	.filter-item-name { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.filter-tag {
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted-foreground) 12%, transparent);
		padding: 1px 6px;
		border-radius: 999px;
		flex-shrink: 0;
	}
	.link-btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 3px 10px;
		border: 1px solid var(--border);
		border-radius: 999px;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 35%, transparent);
		cursor: pointer;
		transition: border-color 120ms ease, color 120ms ease;
	}
	.link-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--foreground); }
	.link-btn:disabled { opacity: 0.6; cursor: default; }
	.link-btn :global(svg) { flex-shrink: 0; }
	.board-title {
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.35rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
	}
	.crumb {
		background: transparent;
		border: none;
		padding: 0;
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.35rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease;
	}
	.crumb:hover { color: var(--foreground); }
	.board-head-left :global(.crumb-sep) { color: var(--muted-foreground); opacity: 0.6; }
	.crumb-current {
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.35rem;
		font-weight: 700;
		letter-spacing: -0.01em;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.board-count {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		padding: 2px 8px;
		border-radius: 999px;
		flex-shrink: 0;
	}

	.board-new {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		height: 34px;
		padding: 0 14px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
		transition: filter 120ms ease;
	}
	.board-new:hover { filter: brightness(1.08); }

	/* Body — full-width scroll container; inner content centered at 1240px to
	   match every other Studio-v2 surface (the mockup's `.wrap`). */
	.board-body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 24px 24px 40px;
		display: flex;
		flex-direction: column;
	}
	.board-wrap {
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 22px;
	}

	.board-section { display: flex; flex-direction: column; gap: 10px; flex-shrink: 0; }
	.section-label {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--muted-foreground);
	}
	.section-count {
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		padding: 1px 6px;
		border-radius: 999px;
		letter-spacing: 0;
	}
	.section-label :global(.section-icon) { color: var(--warning); }

	/* Collapsible section headers — click to fold the section away. */
	.section-toggle {
		background: transparent;
		border: none;
		cursor: pointer;
		padding: 2px 0;
		margin: 0;
		width: fit-content;
		transition: color 120ms ease;
	}
	.section-toggle:hover { color: var(--foreground); }
	.section-toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 4px; }
	.section-toggle :global(.section-caret) {
		color: var(--muted-foreground);
		opacity: 0.7;
		transition: transform 140ms ease;
	}
	.section-toggle :global(.section-caret.collapsed) { transform: rotate(-90deg); }

	/* Project grid */
	.proj-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
		gap: 12px;
	}
	.proj-card {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 14px 16px;
		min-width: 0;
		max-width: 100%;
		overflow: hidden;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		text-align: left;
		cursor: pointer;
		transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease;
	}
	.proj-card:hover {
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
		box-shadow: var(--shadow-m), 0 0 0 1px color-mix(in oklch, var(--primary) 12%, transparent);
		transform: translateY(-1px);
	}
	.proj-card:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.proj-top { display: flex; align-items: center; gap: 8px; min-width: 0; }
	.proj-icon { display: flex; color: var(--muted-foreground); flex-shrink: 0; }
	.proj-name {
		flex: 1;
		min-width: 0;
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.proj-running {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--primary);
		flex-shrink: 0;
	}
	.proj-latest {
		max-width: 100%;
		font-size: 0.78rem;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.proj-stats {
		display: flex;
		align-items: center;
		gap: 5px;
		min-width: 0;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}
	.proj-stats .sep { opacity: 0.4; }
	.proj-count-pill {
		font-weight: 600;
		color: var(--foreground);
	}
	.proj-device {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		min-width: 0;
		color: var(--muted-foreground);
	}
	.proj-device.offline { opacity: 0.6; }
	.proj-device :global(svg) { flex-shrink: 0; }
	.proj-device-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 120px;
	}
	.proj-card :global(.proj-chevron) { margin-left: auto; color: var(--muted-foreground); opacity: 0.5; }

	/* Dense session rows */
	.row-list {
		display: flex;
		flex-direction: column;
		flex-shrink: 0;
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius, 0.75rem);
		overflow: hidden;
		background: var(--card);
	}
	.row {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 9px 14px;
		min-height: 56px;
		cursor: pointer;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		transition: background-color 120ms ease;
	}
	.row:last-child { border-bottom: none; }
	.row:hover { background: var(--hover-overlay); }
	.row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.row.active { background: var(--selection-bg, color-mix(in oklch, var(--primary) 10%, transparent)); box-shadow: inset 3px 0 0 var(--primary); }
	.row.deleting { background: color-mix(in oklch, var(--destructive) 8%, transparent); box-shadow: inset 3px 0 0 var(--destructive); }

	.row-status { display: flex; align-items: center; flex-shrink: 0; }
	.dot { width: 8px; height: 8px; border-radius: 50%; display: block; }
	.dot-streaming { background: var(--primary); animation: pulse 2s ease-in-out infinite; }
	.dot-attention { background: var(--warning); animation: pulse 1.5s ease-in-out infinite; }
	.dot-idle { background: color-mix(in oklch, var(--muted-foreground) 45%, transparent); }
	@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

	.awaiting-pill {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		margin-left: 8px;
		padding: 3px 9px;
		border-radius: 999px;
		border: 1px solid color-mix(in oklch, var(--warning) 45%, transparent);
		background: color-mix(in oklch, var(--warning) 14%, transparent);
		color: var(--warning);
		font-size: 0.72rem;
		font-weight: 600;
		line-height: 1;
		cursor: pointer;
		animation: pulse 1.8s ease-in-out infinite;
	}
	.awaiting-pill:hover { background: color-mix(in oklch, var(--warning) 24%, transparent); }
	@media (prefers-reduced-motion: reduce) { .awaiting-pill { animation: none; } }

	.row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
	.row-line1 { display: flex; align-items: center; gap: 8px; }
	.row-title {
		flex: 1;
		min-width: 0;
		font-size: 0.85rem;
		font-weight: 500;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-time { flex-shrink: 0; font-size: 0.6875rem; color: var(--muted-foreground); opacity: 0.75; }

	.row-line2 {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		min-width: 0;
		overflow: hidden;
	}
	.row-line2 .sep { opacity: 0.4; }
	.row-line2 .muted { font-style: italic; opacity: 0.7; }
	.row-stat { white-space: nowrap; }
	.row-proj {
		flex-shrink: 0;
		max-width: 150px;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		padding: 1px 7px;
		border-radius: 999px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-branch {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		max-width: 160px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: color-mix(in oklch, var(--primary) 85%, var(--muted-foreground));
	}
	.row-branch :global(svg) { flex-shrink: 0; }

	.row-automation {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		flex-shrink: 0;
		padding: 1px 7px;
		border-radius: 999px;
		white-space: nowrap;
		color: color-mix(in oklch, var(--info) 80%, var(--muted-foreground));
		background: color-mix(in oklch, var(--info) 14%, transparent);
	}
	.row-automation :global(svg) { flex-shrink: 0; }

	.row-device {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		flex-shrink: 0;
		max-width: 160px;
		padding: 1px 7px;
		border-radius: 999px;
		color: color-mix(in oklch, var(--primary) 80%, var(--muted-foreground));
		background: color-mix(in oklch, var(--primary) 14%, transparent);
	}
	.row-device.offline {
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
	}
	.row-device :global(svg) { flex-shrink: 0; }
	.row-device-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.device-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		flex-shrink: 0;
		background: var(--muted-foreground);
	}
	.device-dot.online {
		background: color-mix(in oklch, var(--primary) 70%, #22c55e);
		box-shadow: 0 0 4px color-mix(in oklch, var(--primary) 50%, #22c55e);
	}

	.row-actions { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
	.row-action {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		opacity: 0;
		transition: opacity 120ms ease, color 120ms ease, background-color 120ms ease;
	}
	.row:hover .row-action { opacity: 1; }
	.row-action:hover { color: var(--foreground); background: color-mix(in oklch, var(--muted) 60%, transparent); }
	.row-action.fav.is-fav { opacity: 1; color: var(--warning); }
	.row-action.fav:hover { color: var(--warning); }
	.row-action.danger { opacity: 1; color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.row-action.danger:hover { background: color-mix(in oklch, var(--destructive) 20%, transparent); }
	.row-action:disabled { opacity: 0.5; cursor: not-allowed; }

	/* Empty */
	.board-empty {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 14px;
		color: var(--muted-foreground);
		text-align: center;
		padding: 48px 16px;
	}
	.board-empty :global(svg) { opacity: 0.5; }
	.board-empty-title { font-size: 0.9rem; }

	/* Touch / mobile */
	/* Mobile: drop the header's right cluster (device filter / link-project
	   dropdown) onto its own row so it never crowds the title or the project
	   breadcrumb. 1080px matches the app-wide mobile cutoff (foldables included). */
	@media (max-width: 1080px) {
		.board-header { flex-wrap: wrap; row-gap: 10px; }
		.board-head-right { width: 100%; margin-left: 0; justify-content: center; }
	}

	/* Touch / mobile-width: row actions can't be hover-revealed — keep them visible.
	   (hover: none) covers real touch devices incl. unfolded foldables; 1080px
	   matches the app-wide mobile cutoff so the desktop resize-test path works too. */
	@media (hover: none), (max-width: 1080px) {
		.row-action { opacity: 0.7; }
	}
	@media (max-width: 639px) {
		.board-header { padding: 12px 16px; }
		.board-body { padding: 12px 16px 28px; }
		.proj-grid { grid-template-columns: 1fr; }
		.row { min-height: 64px; padding: 11px 14px; gap: 11px; }
		.row-title { font-size: 0.9rem; }
		.row-line2 { font-size: 0.72rem; gap: 7px; }
		.row-action { width: 38px; height: 38px; opacity: 0.8; }
	}

	@media (prefers-reduced-motion: reduce) {
		.proj-card { transition: none; }
		.section-toggle :global(.section-caret) { transition: none; }
		.dot-streaming, .dot-attention { animation: none; }
	}
</style>
