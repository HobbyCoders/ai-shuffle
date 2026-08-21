<script lang="ts">
	/**
	 * Sidebar - Studio Noir session navigator.
	 *
	 * A 256px black plane: brand header + new session, ghost rows (search and the
	 * project / device filters), a scrollable list of three-line session cards
	 * grouped Active / Settled, the archived shelf, and a footer icon rail that
	 * switches the MainPanel deck views.
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
	import { onDestroy, type Snippet } from 'svelte';
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
	import { sessions, projects, clusterProjects } from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import SessionCard from './SessionCard.svelte';
	import {
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
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: ViewType) => void;
		onOpenSpotlight?: () => void;
		/** Extra control for the header row (the drawer's close button). */
		headerAction?: Snippet;
	}

	let {
		isAdmin = false,
		onNewSession,
		onOpenSession,
		onOpenFeature,
		onOpenSpotlight,
		headerAction
	}: Props = $props();

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
	});

	// ---------------------------------------------------------------- data
	const activeId = $derived($activeSession.sessionId);
	const projectsById = $derived(new Map($projects.map((p) => [p.id, p])));
	const liveKeys = $derived(liveProjectKeys($projects, $clusterProjects));
	const sidebarSessions = $derived($sessions.filter((s) => isVisibleSidebarSession(s, liveKeys)));

	function isStreaming(id: string): boolean {
		if ($activeSession.sessionId === id) return $activeSession.isStreaming;
		return $streamingSessions.has(id);
	}

	function projectLabel(s: Session): string {
		if (s.project_name) return s.project_name;
		if (s.project_id) return projectsById.get(s.project_id)?.name || 'Ungrouped';
		return 'Ungrouped';
	}

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
	interface CardRow { session: Session; status: SessionCardStatus }

	const rows = $derived.by((): CardRow[] =>
		visibleSessions
			.filter((s) => !isArchived(s))
			.map((s) => ({
				session: s,
				status: deriveStatus({
					streaming: isStreaming(s.id),
					awaiting: $backgroundPermissions.has(s.id),
					isActive: !!s.is_active
				})
			}))
			.sort((a, b) => sessionTimestamp(b.session) - sessionTimestamp(a.session))
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
			.sort((a, b) => sessionTimestamp(b) - sessionTimestamp(a))
			.slice(0, ARCHIVED_LIMIT)
			.map((s) => ({ session: s, status: 'idle' as SessionCardStatus }))
	);
	const archivedCount = $derived(visibleSessions.filter(isArchived).length);
	let archivedOpen = $state(false);

	// Keep the ticker in step with what's actually on screen.
	$effect(() => {
		retick(activeRows.some((row) => row.status === 'working' || row.status === 'await'));
	});

	// --- footer rail -------------------------------------------------------
	const BUILD_MEMBERS: ViewType[] = ['build', 'profile', 'mcp', 'plugins', 'hooks'];
	const currentType = $derived($activeView?.type ?? null);

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

	function handleWindowClick(e: MouseEvent) {
		const t = e.target as Node;
		if (projectMenuOpen && projectMenuRef && !projectMenuRef.contains(t)) projectMenuOpen = false;
		if (deviceMenuOpen && deviceMenuRef && !deviceMenuRef.contains(t)) deviceMenuOpen = false;
	}

	function handleWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') closeMenus();
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
		{#if activeRows.length > 0}
			<button class="grouplabel" onclick={() => toggleGroup('active')} aria-expanded={!collapsedGroups.active}>
				Active <span class="count">{activeRows.length}</span>
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
							onOpen={onOpenSession}
						/>
					{/each}
				</div>
			{/if}
			<div class="divider" role="separator"></div>
		{/if}

		<button class="grouplabel" onclick={() => toggleGroup('settled')} aria-expanded={!collapsedGroups.settled}>
			Settled <span class="count">{settledRows.length}</span>
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
							onOpen={onOpenSession}
						/>
					{/each}
				</div>
			{/if}
		{/if}
	</div>

	<footer class="sb-foot">
		{#each railItems as item (item.label)}
			<button class="railbtn" class:on={item.on} aria-label={item.label} onclick={() => pickFeature(item.type)}>
				<span class="tip">{item.label}</span>
				<item.icon size={16} strokeWidth={1.85} />
			</button>
		{/each}
	</footer>
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
	.divider { height: 1px; background: var(--sidebar-border); margin: 8px 8px 0; }
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

	/* Footer icon rail */
	.sb-foot {
		flex: none;
		border-top: 1px solid var(--sidebar-border);
		padding: 8px;
		display: flex;
		align-items: center;
		gap: 0;
	}
	.railbtn {
		position: relative;
		width: 30px;
		height: 30px;
		border: none;
		background: transparent;
		border-radius: 8px;
		display: grid;
		place-items: center;
		color: var(--subtle-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.railbtn:hover { background: var(--sidebar-accent); color: var(--foreground); }
	.railbtn.on { color: var(--foreground); background: var(--sidebar-accent); }
	.tip {
		position: absolute;
		bottom: calc(100% + 8px);
		left: 50%;
		transform: translateX(-50%);
		padding: 4px 8px;
		border-radius: 7px;
		font-size: 11.5px;
		line-height: 1.35;
		white-space: nowrap;
		color: var(--popover-foreground);
		background: var(--glass);
		backdrop-filter: blur(14px) saturate(150%);
		-webkit-backdrop-filter: blur(14px) saturate(150%);
		border: 1px solid var(--border);
		box-shadow: inset 0 1px 0 0 var(--glass-highlight);
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.12s ease;
		z-index: 30;
	}
	.railbtn:hover .tip { opacity: 1; }

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* The same rail serves the desktop sidebar and the mobile drawer; on
	   handheld widths enlarge its controls to clear the 44px touch minimum. */
	@media (hover: none), (max-width: 1080px) {
		.iconbtn { width: 40px; height: 40px; }
		.ghostrow { height: 44px; }
		.sb-foot { gap: 2px; flex-wrap: wrap; justify-content: space-between; }
		.railbtn { width: 44px; height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.iconbtn, .ghostrow, .shelf, .railbtn, .tip { transition: none; }
	}
</style>
