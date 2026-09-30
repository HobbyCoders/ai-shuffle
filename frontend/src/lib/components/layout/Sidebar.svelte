<script lang="ts">
	import { sidebarGroups } from '$lib/stores/expansion-preferences';
	/** Studio Noir navigator: pinned Assistant, unified conversation history,
	 * explicit project/device scopes, and a new-conversation capability picker.
	 * Voice is a call action, not a mutually exclusive history lane. */
	import { onDestroy, onMount, untrack, type Snippet } from 'svelte';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
	import { assistantState, refreshAssistant } from '$lib/stores/assistant';
	import {
		Search,
		FolderClosed,
		MonitorSmartphone,
		ChevronDown,
		ChevronRight,
		Check,
		SquarePen,
		X,
		Files,
		StickyNote,
		Hammer,
		CalendarClock,
		FolderKanban,
		BarChart3,
		Settings,
		BookOpen
	} from 'lucide-svelte';
	import type { Session } from '$lib/api/client';
	import { getPullRequests, type PullRequest } from '$lib/api/github';
	import {
		appData,
		sessions,
		projects,
		clusterProjects,
		sessionsHasMore,
		sessionsLoadingMore
	} from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { preloadPanelForView } from '$lib/components/deck/lazy/panels';
	import { appMode, sessionMode, type SessionMode } from '$lib/stores/sessionMode.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import NotificationCenter from '$lib/components/NotificationCenter.svelte';
	import DesktopUpdateButton from './sidebar/DesktopUpdateButton.svelte';
	import SessionCard from './SessionCard.svelte';
	import SessionModeSwitcher from './SessionModeSwitcher.svelte';
	import WorkspaceNav from './sidebar/WorkspaceNav.svelte';
	import WorkspaceCustomize from './sidebar/WorkspaceCustomize.svelte';
	import type { WorkspaceNavItem } from './sidebar/workspace-items';
	import {
		branchLabel,
		deriveStatus,
		sessionTimestamp,
		sessionDeviceId,
		sessionModeOf,
		projectKey,
		isVisibleSidebarSession,
		isArchived,
		liveProjectKeys,
		UNGROUPED_PROJECT,
		type SessionCardStatus
	} from './session-card-data';

	interface Props {
		isAdmin?: boolean;
		onNewSession: () => void;
		/** Settled the session currently open in the main panel — same as New Session. */
		onSettledOpenSession: () => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenSpotlight?: () => void;
		/** Notified after the lane switcher has already written `appMode`.
		 *  Optional: no host threads it today (see the mode-lane note above). */
		onSelectMode?: (mode: SessionMode) => void;
		/** Extra control for the header row (the drawer's close button). */
		headerAction?: Snippet;
	}

	let {
		isAdmin = false,
		onNewSession,
		onSettledOpenSession,
		onOpenSession,
		onOpenFeature,
		onOpenSpotlight,
		onSelectMode,
		headerAction
	}: Props = $props();

	// Statuses whose elapsed timer ticks live (1s) rather than showing a coarse age.
	const LIVE_TICK_STATUSES: ReadonlySet<string> = new Set([
		'working',
		'needs_permission',
		'has_question',
		'await'
	]);

	// The list starts capped; "All sessions" grows it in place (the board is gone).
	const SETTLED_LIMIT = 30;
	const SHOW_MORE_STEP = 50;
	const ARCHIVED_LIMIT = 30;

	const collapsedGroups = $derived($sidebarGroups);
	function toggleGroup(name: string) {
		sidebarGroups.set({ ...collapsedGroups, [name]: !collapsedGroups[name] });
	}

	// ---------------------------------------------------------------- clock
	// One shared ticker drives every card's timer: 1s while something is live,
	// a lazy 30s otherwise (settled cards only show coarse ages).
	let now = $state(Date.now());
	let tickTimer: ReturnType<typeof setInterval> | null = null;
	let tickFast = false;

	function retick(fast: boolean) {
		if (tickTimer && tickFast === fast) return;
		if (tickTimer) clearInterval(tickTimer);
		tickFast = fast;
		tickTimer = setInterval(() => { now = Date.now(); }, fast ? 1000 : 30000);
	}

	onDestroy(() => {
		if (tickTimer) clearInterval(tickTimer);
		if (prRefreshTimer) clearTimeout(prRefreshTimer);
	});

	// ---------------------------------------------------------------- data
	const activeId = $derived($activeSession.sessionId);
	const projectsById = $derived(new Map($projects.map((p) => [p.id, p])));
	const liveKeys = $derived(liveProjectKeys($projects, $clusterProjects));
	// One lane at a time. This is the ONLY place the mode partition is applied:
	// the filter options, `rows`, the three groups and the PR-branch fetch set
	// all derive from `sidebarSessions`, so a row excluded here is absent from
	// every one of them.
	const sidebarSessions = $derived(
		$sessions.filter((s) => isVisibleSidebarSession(s, liveKeys))
	);

	// User-wide run and task updates own sidebar state, not transcript replay.
	// A cached turn/task can be out of date when a session is selected. Only a
	// local send bridges the wait for the server's run-start broadcast.
	const activeStreamingId = $derived($activeSession.optimisticStreaming ? $activeSession.sessionId : null);
	const streamingMap = $derived($streamingSessions);

	function isStreaming(id: string): boolean {
		return streamingMap.has(id) || activeStreamingId === id;
	}

	function projectLabel(s: Session): string {
		if (s.project_name) return s.project_name;
		if (s.project_id) return projectsById.get(s.project_id)?.name || 'Ungrouped';
		return 'Ungrouped';
	}

	// --- open-PR detection ---------------------------------------------------
	// A session whose logical branch has an open PR shows "PR Opened" instead of
	// Done. Open PRs are fetched per project and cached until the session list
	// changes; a failed fetch just means no PR badge (never an error surface).
	let openPrBranches = $state<Map<string, Set<string>>>(new Map());
	let prFetchToken = 0;

	const prProjects = $derived.by((): { projectId: string; branches: Set<string> }[] => {
		const wanted = new Map<string, Set<string>>();
		for (const s of sidebarSessions) {
			// Persisted PR state is the source of truth; only poll GitHub for
			// rows that have never been written (pre-migration / never reconciled).
			if (s.pr_state) continue;
			const branch = branchLabel(s);
			if (!branch || !s.project_id) continue;
			let set = wanted.get(s.project_id);
			if (!set) wanted.set(s.project_id, (set = new Set()));
			set.add(branch);
		}
		return [...wanted.entries()].map(([projectId, branches]) => ({ projectId, branches }));
	});

	/** Identity of the (project, branch) set the PR badges depend on. */
	const prProjectsKey = $derived(
		prProjects.map((p) => `${p.projectId}\u0000${[...p.branches].sort().join('\u0001')}`).join('\u0002')
	);

	function prMapsEqual(a: Map<string, Set<string>>, b: Map<string, Set<string>>): boolean {
		if (a.size !== b.size) return false;
		for (const [key, branches] of a) {
			const other = b.get(key);
			if (!other || other.size !== branches.size) return false;
			for (const branch of branches) if (!other.has(branch)) return false;
		}
		return true;
	}

	async function refreshOpenPrs() {
		const token = ++prFetchToken;
		const projectIds = prProjects.map((p) => p.projectId);
		const previous = openPrBranches;
		const next = new Map<string, Set<string>>();
		await Promise.all(
			projectIds.map(async (projectId) => {
				try {
					const prs: PullRequest[] = await getPullRequests(projectId, 'open', 100);
					if (token !== prFetchToken) return;
					next.set(projectId, new Set(prs.map((pr) => pr.head_branch)));
				} catch {
					// A transient failure (auth blip, rate limit, offline) must not
					// blank a badge that is already on screen — carry the last known
					// set for this project instead of dropping to "no PR".
					const known = previous.get(projectId);
					if (known) next.set(projectId, known);
				}
			})
		);
		if (token !== prFetchToken) return;
		// Only publish a genuinely different result: a new Map identity would
		// re-derive every card row for nothing.
		if (!prMapsEqual(previous, next)) openPrBranches = next;
	}

	// Refetch only when the set of (project, branch) pairs actually changes.
	//
	// R8: the dedupe key used to be computed and never compared, so this effect
	// re-ran on every $sessions emission (the filter above hands it a new array
	// identity each time) and fired getPullRequests(…, 100) for EVERY project —
	// a GitHub call burst per turn completion. The key is now actually the gate,
	// and changes after the first fetch are debounced.
	const PR_REFRESH_DEBOUNCE_MS = 2000;
	let lastPrKey: string | null = null;
	let prFetchedOnce = false;
	let prRefreshTimer: ReturnType<typeof setTimeout> | null = null;

	$effect(() => {
		const key = prProjectsKey;
		if (key === lastPrKey) return;
		lastPrKey = key;
		if (prRefreshTimer) {
			clearTimeout(prRefreshTimer);
			prRefreshTimer = null;
		}
		// First real project set: fetch straight away so the badge is not held
		// back by the debounce on first paint. Everything after that trails.
		if (key && !prFetchedOnce) {
			prFetchedOnce = true;
			untrack(() => void refreshOpenPrs());
			return;
		}
		prRefreshTimer = setTimeout(() => {
			prRefreshTimer = null;
			void refreshOpenPrs();
		}, PR_REFRESH_DEBOUNCE_MS);
	});

	// --- filters -----------------------------------------------------------
	let projectFilter = $state<string | null>(null);
	let deviceFilter = $state<string | null>(null);
	let projectMenuOpen = $state(false);
	let deviceMenuOpen = $state(false);
	let projectMenuRef = $state<HTMLDivElement | null>(null);
	let deviceMenuRef = $state<HTMLDivElement | null>(null);

	interface MenuPos { top: number; left: number; width: number }
	let projectMenuPos = $state<MenuPos>({ top: 0, left: 0, width: 200 });
	let deviceMenuPos = $state<MenuPos>({ top: 0, left: 0, width: 200 });

	/** Snapshot a fixed-position anchor under `btn`, kept inside the panel. */
	function anchorBelow(btn: HTMLElement): MenuPos {
		const r = btn.getBoundingClientRect();
		const panel = btn.closest('aside')?.getBoundingClientRect();
		if (!panel) return { top: r.bottom + 4, left: r.left, width: Math.max(180, r.width) };
		const width = Math.min(280, Math.max(180, panel.width - 16));
		const left = Math.min(Math.max(panel.left + 8, r.left), Math.max(panel.left + 8, panel.right - 8 - width));
		return { top: r.bottom + 4, left, width };
	}

	interface FilterOption { id: string; label: string }

	const projectOptions = $derived.by((): FilterOption[] => {
		const map = new Map<string, string>();
		for (const s of sidebarSessions) {
			const key = projectKey(s);
			if (!map.has(key)) map.set(key, key === UNGROUPED_PROJECT ? 'Ungrouped' : projectLabel(s));
		}
		return [...map.entries()]
			.map(([id, label]) => ({ id, label }))
			.sort((a, b) => a.label.localeCompare(b.label));
	});

	const deviceOptions = $derived.by((): FilterOption[] => {
		const map = new Map<string, string>();
		for (const s of sidebarSessions) {
			const id = sessionDeviceId(s);
			if (!map.has(id)) {
				map.set(id, id === '__local__' ? 'This device' : s.device_name || 'Unknown device');
			}
		}
		return [...map.entries()]
			.map(([id, label]) => ({ id, label }))
			.sort((a, b) => a.label.localeCompare(b.label));
	});

	const projectFilterLabel = $derived(
		projectFilter ? projectOptions.find((p) => p.id === projectFilter)?.label ?? 'All projects' : 'All projects'
	);
	const deviceFilterLabel = $derived(
		deviceFilter ? deviceOptions.find((d) => d.id === deviceFilter)?.label ?? 'All devices' : 'All devices'
	);

	// Drop a filter whose target disappeared so the list can never look empty
	// with no visible way back.
	$effect(() => {
		if (projectFilter && !projectOptions.some((p) => p.id === projectFilter)) projectFilter = null;
	});
	// Conversation type no longer filters history. Project scope is explicit.
	$effect(() => {
		if (deviceFilter && !deviceOptions.some((d) => d.id === deviceFilter)) deviceFilter = null;
	});

	const visibleSessions = $derived(
		sidebarSessions.filter(
			(s) =>
				(!projectFilter || projectKey(s) === projectFilter) &&
				(!deviceFilter || sessionDeviceId(s) === deviceFilter)
		)
	);

	// --- grouping ----------------------------------------------------------
	// `ts` is the row's last-activity time, resolved ONCE per row (R2). The
	// comparator used to call sessionTimestamp() — a `Date` parse — on every
	// comparison, i.e. O(n log n) date parses per recompute.
	interface CardRow { session: Session; status: SessionCardStatus; openPr: boolean; ts: number }

	const rows = $derived.by((): CardRow[] =>
		visibleSessions
			.filter((s) => !isArchived(s))
			.map((s) => {
				const branch = branchLabel(s);
				const prSet = branch && s.project_id ? openPrBranches.get(s.project_id) : undefined;
				return {
					session: s,
					openPr: !!(branch && prSet?.has(branch)),
					status: deriveStatus({
						remoteRuntimeState: s.is_remote ? s.remote_runtime_state ?? 'unknown' : null,
						streaming: isStreaming(s.id),
						backgroundWork: !!s.has_running_tasks,
						parked: !!s.parked_task_kind,
						attention: $backgroundPermissions.get(s.id) ?? null,
						prState: s.pr_state ?? null,
						openPr: !s.pr_state && !!(branch && prSet?.has(branch)),
						isActive: !!s.is_active
					}),
					ts: sessionTimestamp(s)
				};
			})
			.sort((a, b) => b.ts - a.ts)
	);

	const activeRows = $derived(rows.filter((r) => r.session.is_active));
	const settledRows = $derived(rows.filter((r) => !r.session.is_active));
	let settledLimit = $state(SETTLED_LIMIT);
	const visibleSettledRows = $derived(settledRows.slice(0, settledLimit));
	const hiddenSettled = $derived(Math.max(0, settledRows.length - visibleSettledRows.length));
	const fullyExpanded = $derived(hiddenSettled === 0 && settledLimit > SETTLED_LIMIT);

	const archivedRows = $derived.by((): CardRow[] =>
		visibleSessions
			.filter((s) => isArchived(s))
			.map((s) => ({ session: s, status: 'idle' as SessionCardStatus, openPr: false, ts: sessionTimestamp(s) }))
			.sort((a, b) => b.ts - a.ts)
			.slice(0, ARCHIVED_LIMIT)
	);
	const archivedCount = $derived(visibleSessions.filter(isArchived).length);
	let archivedOpen = $state(false);

	// Each lane names its own nothing-here state; "No sessions yet" under a
	// voice-only list reads as though the whole app were empty.
	const emptyLabel = 'No sessions yet';
	onMount(() => { if (isAdmin) void refreshAssistant().catch(() => {}); });

	// Keep the ticker in step with what's actually on screen. The prompt-parked
	// states keep their live timer too — a session blocked on you is still live.
	$effect(() => {
		retick(activeRows.some((row) => LIVE_TICK_STATUSES.has(row.status)));
	});

	// --- workspace nav ------------------------------------------------------
	const BUILD_MEMBERS: ViewType[] = ['build', 'profile', 'mcp', 'plugins', 'hooks'];
	const currentType = $derived($activeView?.type ?? null);

	// Admin gating stays HERE, above the preference layer: WorkspaceNav and the
	// Customize dialog only ever see the destinations this role may open, so a
	// stored order can never surface Settings to a non-admin — and an admin's
	// order survives their role lapsing, because nothing rewrote it.
	const railItems = $derived<WorkspaceNavItem[]>([
		{ id: 'file-browser', label: 'Files', icon: Files, on: currentType === 'file-browser' },
		{ id: 'notes', label: 'Notes', icon: StickyNote, on: currentType === 'notes' },
		{ id: 'build', label: 'Agents & tools', icon: Hammer, on: currentType ? BUILD_MEMBERS.includes(currentType) : false },
		...(isAdmin
			? [{ id: 'automations' as const, label: 'Automations', icon: CalendarClock, on: currentType === 'automations' }]
			: []),
		{ id: 'project', label: 'Projects', icon: FolderKanban, on: currentType === 'project' },
		{ id: 'stats', label: 'Stats', icon: BarChart3, on: currentType === 'stats' },
		{ id: 'docs', label: 'Docs', icon: BookOpen, on: currentType === 'docs' },
		...(isAdmin
			? [{ id: 'settings' as const, label: 'Settings', icon: Settings, on: currentType === 'settings' }]
			: [])
	]);

	let workspaceRef = $state<HTMLDivElement | null>(null);
	let flyoutOpen = $state(false);
	let customizeOpen = $state(false);

	function openCustomize() {
		flyoutOpen = false;
		customizeOpen = true;
	}

	// --- sticky block / scroll state ---------------------------------------
	// The separator under the sticky block is a scroll signal, not decoration:
	// it only appears once the list has actually moved under it.
	let scrollRef = $state<HTMLDivElement | null>(null);
	let scrolled = $state(false);
	/** Panel is fullscreen — the mobile shell's drawer. Drives where the More
	 *  flyout is drawn. Never true on the desktop shell, at any window width:
	 *  there the sidebar is a docked (or overlaid) panel with room beside it. */
	const fullscreen = $derived(platform.isMobile);

	function handleScroll() {
		scrolled = (scrollRef?.scrollTop ?? 0) > 2;
		// Fixed popovers hold a snapshot of their anchor (see the header note).
		if (projectMenuOpen || deviceMenuOpen || flyoutOpen) closeMenus();
	}

	// --- menu plumbing -----------------------------------------------------
	function closeMenus() {
		projectMenuOpen = false;
		deviceMenuOpen = false;
		flyoutOpen = false;
	}

	function toggleProjectMenu(e: MouseEvent) {
		const next = !projectMenuOpen;
		closeMenus();
		if (!next) return;
		projectMenuPos = anchorBelow(e.currentTarget as HTMLElement);
		projectMenuOpen = true;
	}

	function toggleDeviceMenu(e: MouseEvent) {
		const next = !deviceMenuOpen;
		closeMenus();
		if (!next) return;
		deviceMenuPos = anchorBelow(e.currentTarget as HTMLElement);
		deviceMenuOpen = true;
	}

	/** Both scopes at once — the row's single clear affordance. */
	function clearFilters() {
		projectFilter = null;
		deviceFilter = null;
	}

	// --- delegated card menus ------------------------------------------------
	// Which card has its swipe actions revealed, if any (R15). Every SessionCard
	// used to attach its OWN <svelte:window> click/keydown pair, so a single
	// click anywhere in the app ran one handler per rendered card (60+). The
	// sidebar owns the open-card id instead and closes it from the one listener
	// it already had.
	let openActionsCardId = $state<string | null>(null);

	function closeCardActions() {
		openActionsCardId = null;
	}

	function handleWindowClick(e: MouseEvent) {
		const t = e.target as Node;
		if (projectMenuOpen && projectMenuRef && !projectMenuRef.contains(t)) projectMenuOpen = false;
		if (deviceMenuOpen && deviceMenuRef && !deviceMenuRef.contains(t)) deviceMenuOpen = false;
		// The flyout is a child of `workspaceRef`, so one containment test covers
		// both the More row and the menu it opened.
		if (flyoutOpen && workspaceRef && !workspaceRef.contains(t)) flyoutOpen = false;
		if (openActionsCardId) {
			const el = t instanceof Element ? t : (t as Node)?.parentElement ?? null;
			const frame = el?.closest?.('[data-session-card]') ?? null;
			if (frame?.getAttribute('data-session-card') !== openActionsCardId) closeCardActions();
		}
	}

	function handleWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			closeMenus();
			closeCardActions();
			customizeOpen = false;
		}
	}

	function pickProject(id: string | null) {
		projectFilter = id;
		projectMenuOpen = false;
	}
	function pickDevice(id: string | null) {
		deviceFilter = id;
		deviceMenuOpen = false;
	}
	/** Switch lanes. The sidebar owns the store write, then lands on the new
	 *  lane's compose screen: the session on screen belongs to the lane just
	 *  left and would otherwise sit there while the list beside it no longer
	 *  contains it. */
	function selectMode(mode: SessionMode) {
		if (mode === $sessionMode) return;
		closeMenus();
		appMode.set(mode);
		onSelectMode?.(mode);
		onNewSession();
	}

	function pickFeature(type: ViewType) {
		closeMenus();
		onOpenFeature(type);
	}

	/** Pull the next cursor page and reveal it in place. */
	function loadMoreSessions() {
		settledLimit += SHOW_MORE_STEP;
		void appData.loadMoreSessions();
	}
</script>

<svelte:window onclick={handleWindowClick} onkeydown={handleWindowKeydown} />

<aside class="sidebar grain" aria-label="Sessions">
	<!-- Sticky top block: the brand row and New session travel together and never
	     scroll, so the panel's two constant actions are always in the same place. -->
	<div class="sb-top" class:scrolled>
		<header class="sb-head">
			<div class="brand">
				<span class="brandmark" aria-hidden="true">
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round">
						<path d="M16 3h5v5" /><path d="M4 20 21 3" /><path d="M21 16v5h-5" /><path d="m15 15 6 6" /><path d="M4 4l5 5" />
					</svg>
				</span>
				<span class="wordmark">AI Shuffle</span>
			</div>
			<DesktopUpdateButton />
			{#if onOpenSpotlight}
				<button class="iconbtn" onclick={onOpenSpotlight} title="Search (Cmd/Ctrl+K)" aria-label="Search">
					<Search size={16} strokeWidth={1.85} />
				</button>
			{/if}
			<!-- The bell's only home: it used to sit in both top bars, which meant
			     two copies on mobile and none while the drawer was open. -->
			<NotificationCenter />
			{#if headerAction}
				{@render headerAction()}
			{/if}
		</header>

		{#if isAdmin}
			<button class="assistant-pin" aria-label="Assistant home" class:on={currentType === 'assistant'} aria-current={currentType === 'assistant' ? 'page' : undefined} onclick={() => pickFeature('assistant')}>
				<AgentAvatar avatar={$assistantState?.avatar_id ?? 'robot'} size={32} />
				<span><strong>Assistant</strong><small>{$assistantState?.assistant_name || 'Make yourself at home'}</small></span>
				<span class="home-dot" class:ready={$assistantState?.status === 'ready'} aria-hidden="true"></span>
			</button>
		{/if}
		<!-- One history. The composer type controls capabilities, not a history lane. -->
		<button
			class="ghostrow newrow"
			onclick={onNewSession}
			title="New session (Cmd/Ctrl+N)"
		>
			<SquarePen size={16} strokeWidth={1.85} />
			<span class="label">New session</span>
			<kbd class="kbd shortcut">⌘N</kbd>
		</button>
	</div>

	<div class="sb-scroll" class:edge-guard={fullscreen} bind:this={scrollRef} onscroll={handleScroll}>
		<div class="workspace" bind:this={workspaceRef}>
			<WorkspaceNav
				items={railItems}
				bind:open={flyoutOpen}
				{fullscreen}
				onPick={pickFeature}
				onPreload={preloadPanelForView}
				onCustomize={openCustomize}
			/>
		</div>

		<!-- Scope filters: two ghost buttons on one rule, heading the ledger. -->
		<div class="scope" role="group" aria-label="Scope">
			<!-- Explicit project scope applies to the shared history, independently of composer type. -->
			{#if projectOptions.length > 0}
				<div class="rowmenu" bind:this={projectMenuRef}>
					<button
						class="fbtn"
						class:set={!!projectFilter}
						aria-haspopup="menu"
						aria-expanded={projectMenuOpen}
						onclick={toggleProjectMenu}
					>
						<FolderClosed size={14} strokeWidth={1.85} />
						<span class="val">{projectFilterLabel}</span>
						<ChevronDown class="chev" size={12} strokeWidth={2} />
					</button>
					{#if projectMenuOpen}
						<div
							class="menu"
							role="menu"
							style="top:{projectMenuPos.top}px; left:{projectMenuPos.left}px; width:{projectMenuPos.width}px;"
						>
							<button class="menu-item" class:active={!projectFilter} role="menuitem" onclick={() => pickProject(null)}>
								<span class="menu-label">All projects</span>
								{#if !projectFilter}<Check size={14} strokeWidth={2} />{/if}
							</button>
							{#each projectOptions as opt (opt.id)}
								<button class="menu-item" class:active={projectFilter === opt.id} role="menuitem" onclick={() => pickProject(opt.id)}>
									<span class="menu-label">{opt.label}</span>
									{#if projectFilter === opt.id}<Check size={14} strokeWidth={2} />{/if}
								</button>
							{/each}
						</div>
					{/if}
				</div>
			{/if}

			<div class="rowmenu" bind:this={deviceMenuRef}>
				<button
					class="fbtn"
					class:set={!!deviceFilter}
					aria-haspopup="menu"
					aria-expanded={deviceMenuOpen}
					onclick={toggleDeviceMenu}
				>
					<MonitorSmartphone size={14} strokeWidth={1.85} />
					<span class="val">{deviceFilterLabel}</span>
					<ChevronDown class="chev" size={12} strokeWidth={2} />
				</button>
				{#if deviceMenuOpen}
					<div
						class="menu"
						role="menu"
						style="top:{deviceMenuPos.top}px; left:{deviceMenuPos.left}px; width:{deviceMenuPos.width}px;"
					>
						<button class="menu-item" class:active={!deviceFilter} role="menuitem" onclick={() => pickDevice(null)}>
							<span class="menu-label">All devices</span>
							{#if !deviceFilter}<Check size={14} strokeWidth={2} />{/if}
						</button>
						{#each deviceOptions as opt (opt.id)}
							<button class="menu-item" class:active={deviceFilter === opt.id} role="menuitem" onclick={() => pickDevice(opt.id)}>
								<span class="menu-label">{opt.label}</span>
								{#if deviceFilter === opt.id}<Check size={14} strokeWidth={2} />{/if}
							</button>
						{/each}
					</div>
				{/if}
			</div>

			{#if projectFilter || deviceFilter}
				<button class="scope-clear" onclick={clearFilters} title="Clear filters" aria-label="Clear filters">
					<X size={13} strokeWidth={2} />
				</button>
			{/if}
		</div>

		{#if activeRows.length > 0}
			<button class="grouplabel" onclick={() => toggleGroup('active')} aria-expanded={!collapsedGroups.active}>
				Active <span class="count">{activeRows.length}</span>
				<span class="rule" aria-hidden="true"></span>
				<span class="group-chev" class:is-collapsed={collapsedGroups.active}>
					<ChevronDown size={13} strokeWidth={2} />
				</span>
			</button>
			{#if !collapsedGroups.active}
				<div class="cards">
					{#each activeRows as row (row.session.id)}
						<SessionCard
							session={row.session}
							status={row.status}
							openPr={row.openPr}
							projectLabel={projectLabel(row.session)}
							active={row.session.id === activeId}
							{now}
							timestamp={row.ts}
							openActionsId={openActionsCardId}
							onActionsOpenChange={(id) => (openActionsCardId = id)}
							onOpen={onOpenSession}
							onSettled={onSettledOpenSession}
						/>
					{/each}
				</div>
			{/if}
			<div class="divider" role="separator"></div>
		{/if}

		<button class="grouplabel" onclick={() => toggleGroup('settled')} aria-expanded={!collapsedGroups.settled}>
			Settled <span class="count">{settledRows.length}</span>
			<span class="rule" aria-hidden="true"></span>
			<span class="group-chev" class:is-collapsed={collapsedGroups.settled}>
				<ChevronDown size={13} strokeWidth={2} />
			</span>
		</button>
		{#if !collapsedGroups.settled}
			{#if visibleSettledRows.length === 0}
				<p class="empty">{emptyLabel}</p>
			{:else}
				<div class="cards">
					{#each visibleSettledRows as row (row.session.id)}
						<SessionCard
							session={row.session}
							status={row.status}
							openPr={row.openPr}
							projectLabel={projectLabel(row.session)}
							active={row.session.id === activeId}
							{now}
							timestamp={row.ts}
							openActionsId={openActionsCardId}
							onActionsOpenChange={(id) => (openActionsCardId = id)}
							compact
							onOpen={onOpenSession}
						/>
					{/each}
				</div>
			{/if}

			<!-- The board is retired: this shelf grows the list in place instead. -->
			{#if hiddenSettled > 0}
				<button class="shelf" onclick={() => (settledLimit += SHOW_MORE_STEP)}>
					<span class="txt">All sessions ({rows.length})</span>
					<span class="rule" aria-hidden="true"></span>
					<span class="txt">{hiddenSettled} more</span>
					<ChevronDown size={14} strokeWidth={2} />
				</button>
			{:else if $sessionsHasMore}
				<!-- Everything loaded is on screen and the server holds more: the
				     initial load is capped at one page (W1), so fetch the next one
				     on demand instead of paging through the whole history up front. -->
				<button class="shelf" onclick={loadMoreSessions} disabled={$sessionsLoadingMore}>
					<span class="txt">{$sessionsLoadingMore ? 'Loading…' : 'Load older sessions'}</span>
					<span class="rule" aria-hidden="true"></span>
					<ChevronDown size={14} strokeWidth={2} />
				</button>
			{:else if fullyExpanded}
				<button class="shelf" onclick={() => (settledLimit = SETTLED_LIMIT)}>
					<span class="txt">Show fewer</span>
					<span class="rule" aria-hidden="true"></span>
					<ChevronRight size={14} strokeWidth={2} />
				</button>
			{/if}
		{/if}

		{#if archivedCount > 0}
			<button class="shelf" onclick={() => (archivedOpen = !archivedOpen)} aria-expanded={archivedOpen}>
				<span class="txt">Archived ({archivedCount})</span>
				<span class="rule" aria-hidden="true"></span>
				{#if archivedOpen}
					<ChevronDown size={14} strokeWidth={2} />
				{:else}
					<ChevronRight size={14} strokeWidth={2} />
				{/if}
			</button>
			{#if archivedOpen}
				<div class="cards">
					{#each archivedRows as row (row.session.id)}
						<SessionCard
							session={row.session}
							status={row.status}
							openPr={row.openPr}
							projectLabel={projectLabel(row.session)}
							active={row.session.id === activeId}
							{now}
							timestamp={row.ts}
							openActionsId={openActionsCardId}
							onActionsOpenChange={(id) => (openActionsCardId = id)}
							compact
							onOpen={onOpenSession}
						/>
					{/each}
				</div>
			{/if}
		{/if}
	</div>

	<!-- Mode dock: the lane switcher on a raised plate at the panel's bottom edge,
	     where a thumb reaches it and it no longer competes with the brand row. -->
	<footer class="sb-dock">
		<p class="dock-label">New conversation</p>
		<SessionModeSwitcher value={$sessionMode} onSelect={selectMode} />
	</footer>

	{#if customizeOpen}
		<WorkspaceCustomize items={railItems} onClose={() => (customizeOpen = false)} />
	{/if}
</aside>

<style>
 .assistant-pin { display:flex; align-items:center; gap:10px; width:100%; margin:8px 0 6px; padding:10px; border:1px solid var(--sidebar-border); background:color-mix(in oklch,var(--primary) 5%,var(--sidebar)); color:var(--sidebar-foreground); border-radius:9px; text-align:left; cursor:pointer; min-height:58px; transition:background .2s,border-color .2s; }
 .assistant-pin>span:not(.home-dot) { flex:1; min-width:0; }
 .assistant-pin strong { display:block; font-size:12px; font-weight:550; }
 .assistant-pin small { display:block; font-size:10px; margin-top:3px; color:var(--sidebar-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
 .assistant-pin:hover,.assistant-pin.on { background:color-mix(in oklch,var(--primary) 10%,var(--sidebar)); border-color:color-mix(in oklch,var(--primary) 45%,var(--sidebar-border)); }
 .assistant-pin:focus-visible { outline:2px solid var(--ring); outline-offset:2px; }
 .assistant-pin:active { transform:translateY(1px); }
 .home-dot { width:5px; height:5px; border-radius:50%; background:var(--sidebar-muted); }
 .home-dot.ready { background:var(--primary); }
 .dock-label { margin:0 0 8px 3px; color:var(--sidebar-muted); font-size:10px; }

	.sidebar {
		position: relative;
		width: 100%;
		height: 100%;
		background: var(--sidebar);
		display: flex;
		flex-direction: column;
		min-height: 0;
		color: var(--sidebar-foreground);
	}

	/* Sticky top block --------------------------------------------------------
	   Header row + New session, above the scroll region. The separator is drawn
	   on ::after so it can cross-fade in without ever changing the block's
	   height: a border appearing on scroll would shift every row below it. */
	.sb-top {
		position: relative;
		flex: none;
		z-index: 5;
		padding: 0 6px;
		background: var(--sidebar);
	}
	.sb-top::after {
		content: '';
		position: absolute;
		left: 0;
		right: 0;
		top: 100%;
		height: 20px;
		pointer-events: none;
		background:
			linear-gradient(var(--sidebar-border), var(--sidebar-border)) top / 100% 1px no-repeat,
			linear-gradient(color-mix(in oklch, var(--background) 70%, transparent), transparent);
		opacity: 0;
		transition: opacity 0.22s ease;
	}
	.sb-top.scrolled::after { opacity: 1; }

	/* Header */
	.sb-head {
		display: flex;
		align-items: center;
		gap: 2px;
		height: 48px;
		padding: 0 0 0 4px;
		flex: none;
	}
	.brand {
		display: flex;
		align-items: center;
		gap: 9px;
		margin-right: auto;
		min-width: 0;
	}
	.brandmark {
		width: 22px;
		height: 22px;
		border-radius: 7px;
		display: grid;
		place-items: center;
		background: linear-gradient(
			150deg,
			color-mix(in oklch, var(--primary) 82%, transparent),
			color-mix(in oklch, var(--primary) 34%, transparent)
		);
		color: var(--primary-foreground);
		box-shadow: 0 0 0 1px var(--border), 0 0 14px -4px var(--glow);
		flex: none;
	}
	.wordmark {
		font-size: 13.5px;
		font-weight: 600;
		letter-spacing: -0.012em;
		white-space: nowrap;
	}
	.iconbtn {
		width: 28px;
		height: 28px;
		border: none;
		background: transparent;
		border-radius: 8px;
		display: grid;
		place-items: center;
		color: var(--sidebar-muted);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.iconbtn:hover { background: var(--sidebar-accent); color: var(--foreground); }

	/* The bell is shared with the top bars, where it is right-anchored under a
	   wide header. Inside a 220–400px panel that anchor puts most of the panel
	   off the left edge, so pin it to the viewport under the header instead —
	   which also takes it out of reach of any overflow container. */
	.sb-head :global(.notif-panel) {
		position: fixed;
		top: 52px;
		left: 8px;
		right: auto;
		width: min(340px, calc(100vw - 16px));
		max-height: min(440px, calc(100vh - 140px));
	}

	/* Ghost rows */
	.rowmenu { position: relative; }
	.ghostrow {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 9px;
		height: 32px;
		padding: 0 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		color: var(--sidebar-muted);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.ghostrow:hover { background: var(--sidebar-accent); color: var(--foreground); }
	.ghostrow:hover :global(svg) { color: var(--foreground); }
	.ghostrow :global(svg) { color: var(--subtle-foreground); transition: color 0.13s ease; flex: none; }
	.ghostrow .label {
		font-size: 13px;
		margin-right: auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.kbd {
		font-family: var(--font-mono);
		font-size: 10.5px;
		line-height: 1;
		padding: 3.5px 5px;
		border-radius: 5px;
		color: var(--subtle-foreground);
		background: var(--sidebar-accent);
		border: 1px solid var(--sidebar-border);
	}

	/* New session: the first row of the nav, lifted by the accent glyph alone so
	   it keeps the exact row rhythm of the Workspace rows below it. */
	.newrow { color: var(--sidebar-foreground); margin-bottom: 1px; }
	.newrow :global(svg) { color: var(--primary); }
	.newrow:hover { background: color-mix(in oklch, var(--primary) 16%, transparent); }
	.newrow:hover :global(svg) { color: var(--primary); }
	.sb-top.scrolled .newrow { margin-bottom: 6px; }

	/* Scope filters: two ghost buttons side by side on a top rule, with one
	   clear that resets both — a per-filter clear would double the controls in
	   a row that is already two menus wide at 220px. */
	.scope {
		display: flex;
		align-items: center;
		gap: 2px;
		margin: 8px 2px 0;
		padding-top: 8px;
		border-top: 1px solid var(--sidebar-border);
	}
	.fbtn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		height: 28px;
		padding: 0 7px;
		border: none;
		background: transparent;
		border-radius: 7px;
		color: var(--sidebar-muted);
		font-size: 12px;
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.fbtn:hover { background: var(--sidebar-accent); color: var(--foreground); }
	.fbtn[aria-expanded='true'] { background: var(--sidebar-accent-active); color: var(--foreground); }
	.fbtn :global(svg) { flex: none; color: var(--subtle-foreground); transition: color 0.13s ease; }
	.fbtn .val { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	/* An applied filter marks its button so the active scope is legible at a
	   glance: the label goes bright and the glyph takes the accent. */
	.fbtn.set { color: var(--foreground); background: var(--sidebar-accent); }
	.fbtn.set .val { font-weight: 500; }
	.fbtn.set :global(svg:not(.chev)) { color: var(--primary); }
	.scope-clear {
		margin-left: auto;
		flex: none;
		width: 26px;
		height: 26px;
		display: grid;
		place-items: center;
		border: none;
		background: transparent;
		border-radius: 6px;
		color: var(--subtle-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.scope-clear:hover { background: var(--sidebar-accent); color: var(--foreground); }
	.rowmenu { min-width: 0; }

	/* Filter menus. Fixed, not absolute: the buttons live inside the scroll
	   region (see the POPOVER ANCHORING note at the top), and the coordinates
	   come from the button's rect at open time. */
	.menu {
		position: fixed;
		max-height: 320px;
		overflow-y: auto;
		padding: 4px;
		background: color-mix(in oklch, var(--popover) 90%, transparent);
		backdrop-filter: blur(16px) saturate(1.2);
		-webkit-backdrop-filter: blur(16px) saturate(1.2);
		border: 1px solid var(--border);
		border-radius: 12px;
		box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.08), var(--shadow-l);
		transform-origin: 20px 0;
		animation: pop-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
		z-index: 120;
	}
	@keyframes pop-in {
		from { opacity: 0; transform: scale(0.94) translateY(-4px); }
		to { opacity: 1; transform: none; }
	}
	.menu-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 8px;
		border: none;
		background: transparent;
		border-radius: 7px;
		color: var(--foreground);
		font-size: 12.5px;
		text-align: left;
		cursor: pointer;
		transition: background-color 0.12s ease;
	}
	.menu-item:hover { background: var(--accent); }
	.menu-item.active { color: var(--primary); }
	.menu-item :global(svg) { flex: none; color: var(--muted-foreground); }
	.menu-label {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	/* Session list */
	.sb-scroll {
		flex: 1 1 auto;
		min-height: 0;
		overflow-y: auto;
		/* No top padding: New session is the row above, and the list has to
		   continue on the same 1px rhythm rather than after a gap. */
		padding: 0 6px 8px;
		scrollbar-width: thin;
		scrollbar-color: var(--border-strong) transparent;
	}
	/* The mobile drawer is the full screen wide, and Android reserves the
	   outer ~24dp of each edge for its back gesture. A swipe rail flush with
	   that edge got taken for a back swipe, and taps on its outer half went
	   to the system. Hold the rows off the edge so the whole rail, and the
	   place a thumb starts a settle swipe, sit inside the app's own touch area. */
	.sb-scroll.edge-guard { padding-right: 24px; }
	.sb-scroll::-webkit-scrollbar { width: 8px; }
	.sb-scroll::-webkit-scrollbar-thumb {
		background: var(--border);
		border-radius: 8px;
		border: 2px solid transparent;
		background-clip: padding-box;
	}
	.cards { display: flex; flex-direction: column; gap: 1px; }

	.grouplabel {
		display: flex;
		align-items: center;
		gap: 7px;
		width: 100%;
		margin: 0;
		padding: 12px 8px 6px;
		border: none;
		background: transparent;
		font-size: 10.5px;
		font-weight: 600;
		letter-spacing: 0.075em;
		text-transform: uppercase;
		color: var(--subtle-foreground);
		cursor: pointer;
		text-align: left;
		transition: color 0.13s ease;
	}
	.grouplabel:hover { color: var(--muted-foreground); }
	.group-chev {
		margin-left: auto;
		display: inline-flex;
		transition: transform 0.16s ease;
	}
	.group-chev.is-collapsed { transform: rotate(-90deg); }
	.grouplabel .count {
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		letter-spacing: 0;
		color: var(--subtle-foreground);
		opacity: 0.8;
	}
	/* Rule fills the header row after the label so each group reads as a titled
	   divider, breaking up the stacked lists below it. */
	.grouplabel .rule { flex: 1 1 auto; height: 1px; background: var(--sidebar-border); }
	.divider { height: 1px; margin: 8px 8px 0; }
	.empty {
		margin: 0;
		padding: 6px 8px 10px;
		font-size: 12px;
		color: var(--subtle-foreground);
	}

	.shelf {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		margin-top: 12px;
		padding: 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		color: var(--subtle-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.shelf:hover { background: var(--sidebar-accent); color: var(--muted-foreground); }
	.shelf .txt {
		font-size: 11.5px;
		font-weight: 500;
		letter-spacing: 0.02em;
		white-space: nowrap;
	}
	.shelf .rule { flex: 1 1 auto; height: 1px; background: var(--sidebar-border); }
	.shelf :global(svg) { flex: none; }

	/* Workspace nav — labeled rows reusing the ghost-row language, so the
	   sidebar reads as one list of named destinations on desktop and touch
	   alike (no hover-only tooltips). Lives INSIDE the session scroll area so a
	   long workspace list scrolls with the sessions instead of squeezing the
	   scrollable list down to a sliver at the panel's bottom edge. The rows
	   themselves are styled by WorkspaceNav. */
	.workspace { padding-top: 0; }

	/* Mode dock — a raised plate at the bottom edge: top hairline, a slight
	   gradient and an upward shadow, so the lane switcher reads as the floor of
	   the panel rather than one more row in the ledger. */
	.sb-dock {
		flex: none;
		padding: 8px 6px calc(8px + env(safe-area-inset-bottom, 0px));
		background: linear-gradient(
			180deg,
			color-mix(in oklch, var(--sidebar-accent) 60%, var(--sidebar)),
			var(--sidebar)
		);
		border-top: 1px solid var(--sidebar-border);
		box-shadow: 0 -12px 24px -12px oklch(0 0 0 / 0.8), inset 0 1px 0 oklch(1 0 0 / 0.04);
	}

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* The same rows serve the desktop sidebar and the mobile drawer; on the
	   mobile shell — and on any touch-only device — enlarge the controls to
	   clear the 44px touch minimum, and drop the ⌘N hint because there is no
	   keyboard to press it on. */
	@media (hover: none) {
		.iconbtn { width: 40px; height: 40px; }
		.ghostrow { height: 44px; }
		.shortcut { display: none; }
		/* The drawer is fullscreen, so the brand row sits under the status bar /
		   notch unless it clears the inset itself. */
		.sb-top { padding-top: env(safe-area-inset-top, 0px); }
		.sb-head { height: 56px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.iconbtn, .ghostrow, .shelf, .fbtn, .scope-clear, .group-chev, .sb-top::after { transition: none; }
		.menu { animation: none; }
	}
</style>
