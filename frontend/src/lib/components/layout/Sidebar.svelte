<script lang="ts">
	/**
	 * Sidebar - Studio Noir session navigator.
	 *
	 * A 256px black plane: brand header + new session, ghost rows (search and the
	 * project / device filters), a scrollable list of three-line session cards
	 * grouped Active / Settled, the archived shelf, and a collapsible
	 * Workspace section of labeled nav rows that switches the MainPanel
	 * deck views.
	 *
	 * Desktop hosts it beside the main panel; at ≤1080px the same component is
	 * rendered inside AppTopBar's slide-in drawer, so every navigation path is
	 * identical on both.
	 *
	 * Data comes exclusively from the stores the app already loads ($sessions,
	 * streamingSessions, backgroundPermissions). Internal worker sessions, and
	 * chats from deleted projects or devices, are list-hidden and never enter
	 * the filters or either visible group.
	 */
	import { onDestroy, untrack, type Snippet } from 'svelte';
	import {
		Search,
		FolderClosed,
		MonitorSmartphone,
		ChevronDown,
		ChevronRight,
		Check,
		SquarePen,
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
	import SessionCard from './SessionCard.svelte';
	import {
		branchLabel,
		deriveStatus,
		sessionTimestamp,
		sessionDeviceId,
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

	// --- collapsible groups (persisted) -------------------------------------
	const GROUPS_KEY = 'sidebar-collapsed-groups';

	function loadCollapsedGroups(): Record<string, boolean> {
		if (typeof localStorage === 'undefined') return {};
		try {
			return JSON.parse(localStorage.getItem(GROUPS_KEY) || '{}');
		} catch {
			return {};
		}
	}

	let collapsedGroups = $state<Record<string, boolean>>(loadCollapsedGroups());

	function toggleGroup(name: string) {
		collapsedGroups = { ...collapsedGroups, [name]: !collapsedGroups[name] };
		try {
			localStorage.setItem(GROUPS_KEY, JSON.stringify(collapsedGroups));
		} catch {
			// Storage full or blocked — the toggle still works for this session.
		}
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
	const sidebarSessions = $derived($sessions.filter((s) => isVisibleSidebarSession(s, liveKeys)));

	// The open session's streaming flag, narrowed to a SINGLE scalar (R2).
	//
	// `$activeSession` folds ~60×/s while a turn streams. Reading it inside
	// `rows` made the sidebar re-filter, re-map and re-sort the whole session
	// list on every frame. This derived collapses the fold to "which session id
	// is the open one streaming, if any" — a value that only changes when the
	// flag actually flips, so `rows` stays clean across folds.
	//
	// On switch-in, $activeSession.isStreaming is reset to false until the SSE
	// replay re-derives it — trusting it alone flashes the card to Done while
	// the transcript loads. The cross-device map carries the durable busy
	// signal (backend runs table), so OR it in for the open session too.
	const activeStreamingId = $derived($activeSession.isStreaming ? $activeSession.sessionId : null);
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
	interface CardRow { session: Session; status: SessionCardStatus; ts: number }

	const rows = $derived.by((): CardRow[] =>
		visibleSessions
			.filter((s) => !isArchived(s))
			.map((s) => {
				const branch = branchLabel(s);
				const prSet = branch && s.project_id ? openPrBranches.get(s.project_id) : undefined;
				return {
					session: s,
					status: deriveStatus({
						streaming: isStreaming(s.id),
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
			.map((s) => ({ session: s, status: 'idle' as SessionCardStatus, ts: sessionTimestamp(s) }))
			.sort((a, b) => b.ts - a.ts)
			.slice(0, ARCHIVED_LIMIT)
	);
	const archivedCount = $derived(visibleSessions.filter(isArchived).length);
	let archivedOpen = $state(false);

	// Keep the ticker in step with what's actually on screen. The prompt-parked
	// states keep their live timer too — a session blocked on you is still live.
	$effect(() => {
		retick(activeRows.some((row) => LIVE_TICK_STATUSES.has(row.status)));
	});

	// --- workspace nav ------------------------------------------------------
	const BUILD_MEMBERS: ViewType[] = ['build', 'profile', 'mcp', 'plugins', 'hooks'];
	const currentType = $derived($activeView?.type ?? null);

	const WORKSPACE_OPEN_KEY = 'ai-shuffle.sidebar.workspaceOpen';
	function readWorkspaceOpen(): boolean {
		try {
			return localStorage.getItem(WORKSPACE_OPEN_KEY) !== '0';
		} catch {
			return true;
		}
	}
	let workspaceOpen = $state(readWorkspaceOpen());
	function toggleWorkspace() {
		workspaceOpen = !workspaceOpen;
		try {
			localStorage.setItem(WORKSPACE_OPEN_KEY, workspaceOpen ? '1' : '0');
		} catch {
			/* private mode: collapse state just won't persist */
		}
	}

	const railItems = $derived([
		{ label: 'Files', icon: Files, type: 'file-browser' as ViewType, on: currentType === 'file-browser' },
		{ label: 'Notes', icon: StickyNote, type: 'notes' as ViewType, on: currentType === 'notes' },
		{ label: 'Build', icon: Hammer, type: 'build' as ViewType, on: currentType ? BUILD_MEMBERS.includes(currentType) : false },
		...(isAdmin
			? [{ label: 'Automations', icon: CalendarClock, type: 'automations' as ViewType, on: currentType === 'automations' }]
			: []),
		{ label: 'Projects', icon: FolderKanban, type: 'project' as ViewType, on: currentType === 'project' },
		{ label: 'Stats', icon: BarChart3, type: 'stats' as ViewType, on: currentType === 'stats' },
		{ label: 'Docs', icon: BookOpen, type: 'docs' as ViewType, on: currentType === 'docs' },
		...(isAdmin
			? [{ label: 'Settings', icon: Settings, type: 'settings' as ViewType, on: currentType === 'settings' }]
			: [])
	]);

	// --- menu plumbing -----------------------------------------------------
	function closeMenus() {
		projectMenuOpen = false;
		deviceMenuOpen = false;
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
	<header class="sb-head">
		<div class="brand">
			<span class="brandmark" aria-hidden="true">
				<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round">
					<path d="M16 3h5v5" /><path d="M4 20 21 3" /><path d="M21 16v5h-5" /><path d="m15 15 6 6" /><path d="M4 4l5 5" />
				</svg>
			</span>
			<span class="wordmark">AI Shuffle</span>
		</div>
		<button class="iconbtn" onclick={onNewSession} title="New session (Cmd/Ctrl+N)" aria-label="New session">
			<SquarePen size={16} strokeWidth={1.85} />
		</button>
		{#if headerAction}
			{@render headerAction()}
		{/if}
	</header>

	<div class="sb-rows">
		{#if onOpenSpotlight}
			<button class="ghostrow" onclick={onOpenSpotlight}>
				<Search size={16} strokeWidth={1.85} />
				<span class="label">Search</span>
				<kbd class="kbd">⌘K</kbd>
			</button>
		{/if}

		<div class="rowmenu" bind:this={projectMenuRef}>
			<button
				class="ghostrow"
				class:active={!!projectFilter}
				aria-haspopup="menu"
				aria-expanded={projectMenuOpen}
				onclick={() => { const next = !projectMenuOpen; closeMenus(); projectMenuOpen = next; }}
			>
				<FolderClosed size={16} strokeWidth={1.85} />
				<span class="label">{projectFilterLabel}</span>
				<ChevronDown class="chev" size={14} strokeWidth={2} />
			</button>
			{#if projectMenuOpen}
				<div class="menu" role="menu">
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

		<div class="rowmenu" bind:this={deviceMenuRef}>
			<button
				class="ghostrow"
				class:active={!!deviceFilter}
				aria-haspopup="menu"
				aria-expanded={deviceMenuOpen}
				onclick={() => { const next = !deviceMenuOpen; closeMenus(); deviceMenuOpen = next; }}
			>
				<MonitorSmartphone size={16} strokeWidth={1.85} />
				<span class="label">{deviceFilterLabel}</span>
				<ChevronDown class="chev" size={14} strokeWidth={2} />
			</button>
			{#if deviceMenuOpen}
				<div class="menu" role="menu">
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
	</div>
	<div class="sb-scroll">
		<div class="workspace">
			<button class="grouplabel ws-head" onclick={toggleWorkspace} aria-expanded={workspaceOpen}>
				Workspace
				<span class="rule" aria-hidden="true"></span>
				<span class="group-chev" class:is-collapsed={!workspaceOpen}>
					<ChevronDown size={13} strokeWidth={2} />
				</span>
			</button>
			{#if workspaceOpen}
				{#each railItems as item (item.label)}
					<!--
						Intent prefetch: `pointerenter` fires once per genuine entry into the
						row (not per render, not per scroll tick, and — unlike `pointermove`
						or `pointerover` — not repeatedly while the pointer sits inside), and
						`focus` gives keyboard and screen-reader users the identical warm-up.
						`preloadPanelForView` dedupes against the resolved/in-flight chunk, so
						re-entering a row costs nothing beyond the call itself.
					-->
					<button
						class="ghostrow wsrow"
						class:active={item.on}
						onclick={() => pickFeature(item.type)}
						onpointerenter={() => preloadPanelForView(item.type)}
						onfocus={() => preloadPanelForView(item.type)}
					>
						<item.icon size={16} strokeWidth={1.85} />
						<span class="label">{item.label}</span>
					</button>
				{/each}
			{/if}
		</div>
		<div class="divider" role="separator"></div>
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
				<p class="empty">No sessions yet</p>
			{:else}
				<div class="cards">
					{#each visibleSettledRows as row (row.session.id)}
						<SessionCard
							session={row.session}
							status={row.status}
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

</aside>

<style>
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

	/* Header */
	.sb-head {
		display: flex;
		align-items: center;
		gap: 8px;
		height: 52px;
		padding: 0 8px 0 12px;
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

	/* Ghost rows */
	.sb-rows { padding: 2px 8px 6px; flex: none; }
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
	/* An applied filter marks its row so the active scope is legible at a glance
	   (mirrors t3-code's filled filter glyph). Uses the stronger accent-active
	   tier so it reads a notch above a plain hover, and holds it on hover. */
	.ghostrow.active,
	.ghostrow.active:hover { background: var(--sidebar-accent-active); color: var(--foreground); }
	.ghostrow.active :global(svg) { color: var(--primary); }
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

	/* Filter menus */
	.menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		max-height: 320px;
		overflow-y: auto;
		padding: 4px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: var(--shadow-l);
		z-index: 60;
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
		padding: 4px 6px 8px;
		scrollbar-width: thin;
		scrollbar-color: var(--border-strong) transparent;
	}
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
	   alike (no hover-only tooltips). Lives INSIDE the session scroll area so
	   an expanded Workspace scrolls with the sessions instead of squeezing
	   the scrollable list down to a sliver at the panel's bottom edge. */
	.ws-head { padding-top: 6px; padding-bottom: 4px; }
	.wsrow { height: 30px; }
	.wsrow.active,
	.wsrow.active:hover { background: var(--sidebar-accent-active); color: var(--foreground); }
	.wsrow.active :global(svg) { color: var(--primary); }

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* The same workspace rows serve the desktop sidebar and the mobile drawer;
	   on handheld widths enlarge the controls to clear the 44px touch minimum. */
	@media (hover: none), (max-width: 1080px) {
		.iconbtn { width: 40px; height: 40px; }
		.ghostrow { height: 44px; }
		.wsrow { height: 40px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.iconbtn, .ghostrow, .shelf, .ws-head, .group-chev { transition: none; }
	}
</style>