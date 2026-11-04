<script lang="ts">
	import { sidebarGroups } from '$lib/stores/expansion-preferences';
	/**
	 * Sidebar — the rail and the session ledger.
	 *
	 * Left: an icon rail (SidebarRail) holding New, the destinations the user
	 * put there, More and Settings. Right: one column with the mode toggle and
	 * header buttons, search and filter, and the ledger itself: Needs you,
	 * Active and Settled.
	 *
	 * Rows follow the inbox model. Swipe left settles an active session (or
	 * brings a settled one back); swipe right archives. A long press starts a
	 * multi-select whose bar offers the same moves in bulk. On desktop the
	 * same moves sit in each row's hover actions.
	 *
	 * Overlays (More, the filter, Customize) live inside the panel, so on the
	 * phone they ride with the drawer and on desktop they never cover the chat.
	 */
	import { onDestroy, onMount, untrack, type Snippet } from 'svelte';
	import { refreshAgentIdentities } from '$lib/stores/agent-identities';
	import {
		Search,
		ListFilter,
		ChevronDown,
		ChevronRight,
		Loader2,
		SlidersHorizontal,
		RefreshCw,
		X,
		Check,
		Archive,
		Undo2,
		Files,
		StickyNote,
		SquarePen,
		Bot,
		CalendarClock,
		Folder,
		BarChart3,
		GitPullRequest,
		Settings,
		BookOpen,
		SquareTerminal
	} from 'lucide-svelte';
	import type { Session } from '$lib/api/client';
	import { getPullRequests, type PullRequest } from '$lib/api/github';
	import {
		appData,
		sessions,
		projects,
		clusterProjects,
		projectCatalogReady,
		sessionsHasMore
	} from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { privateAccessPending } from '$lib/stores/chat-access';
	import { isTranscriptProseDismissed, proseQuestionDismissals, proseQuestionSessions, withoutSession } from '$lib/stores/session/prose-questions';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { activeView } from '$lib/stores/layout.svelte';
	import type { ViewType } from '$lib/stores/layout.svelte';
	import { preloadPanelForView } from '$lib/components/deck/lazy/panels';
	import { terminals } from '$lib/stores/terminals.svelte';
	import { terminalAccess } from '$lib/terminal/gating';
	import type { SessionMode } from '$lib/stores/sessionMode.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { notify } from '$lib/stores/notifications';
	import { splitWorkspaceItems, WORKSPACE_ITEM_IDS, workspacePrefs, type WorkspaceItemId } from '$lib/stores/sidebar-prefs.svelte';
	import NotificationCenter from '$lib/components/NotificationCenter.svelte';
	import DesktopUpdateButton from './sidebar/DesktopUpdateButton.svelte';
	import SessionCard from './SessionCard.svelte';
	import { backToChats } from '$lib/services/session-navigation';
	import SidebarRail from './sidebar/SidebarRail.svelte';
	import SidebarSheet from './sidebar/SidebarSheet.svelte';
	import WorkspaceCustomize from './sidebar/WorkspaceCustomize.svelte';
	import type { WorkspaceNavItem } from './sidebar/workspace-items';
	import {
		branchLabel,
		deriveStatus,
		sessionTimestamp,
		sessionDeviceId,
		sessionPrState,
		projectKey,
		isVisibleSidebarSession,
		isArchived,
		sidebarGroup,
		lastWorkFailed,
		openSessionNeeds,
		sessionNeedsInput,
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
		/** Retired with the chat/code switch (#371 step 4); kept so callers compile. */
		onSelectMode?: (mode: SessionMode) => void;
		/** Extra control for the header row (the drawer's close button). */
		headerAction?: Snippet;
		/** Leave a page for the chats. The phone's drawer also closes itself. */
		onBackToChats?: () => void;
		/** Only the rail: the open page brings its own navigation column. */
		railOnly?: boolean;
	}

	let {
		isAdmin = false,
		onNewSession,
		onSettledOpenSession,
		onOpenSession,
		onOpenFeature,
		onOpenSpotlight,
		onSelectMode,
		headerAction,
		onBackToChats,
		railOnly = false
	}: Props = $props();

	/** Who gets the phone's few tab seats on a list nobody has reordered. */
	const PHONE_TAB_ORDER: readonly WorkspaceItemId[] = ['project', 'build', 'file-browser', 'notes', 'prs'];

	// Statuses whose elapsed timer ticks live (1s) rather than showing a coarse age.
	// A parked session's total keeps counting while its background work runs.
	const LIVE_TICK_STATUSES: ReadonlySet<string> = new Set([
		'working',
		'parked',
		'needs_permission',
		'has_question',
		'await'
	]);

	// The list starts capped; "All sessions" grows it in place (the board is gone).
	const SETTLED_LIMIT = 30;
	const SHOW_MORE_STEP = 50;
	// Pages one "Load older sessions" click may fetch while looking for a row
	// this list can show. Rows from removed projects or devices, or outside the
	// project/device filter, are dropped here, so one page can add nothing.
	const OLDER_PAGES_PER_CLICK = 5;
	// "Show fewer" can appear under the pointer the moment the last page lands.
	// A click that fast belongs to the load, not to a request to collapse.
	const SHELF_SETTLE_MS = 500;

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
	// Null until the catalog has answered: no project can be proven removed
	// before then, so the first paint already lists every row it will (#804).
	const liveKeys = $derived($projectCatalogReady ? liveProjectKeys($projects, $clusterProjects) : null);
	// Every session, with or without a project, is listed together. This is
	// the one place rows are excluded (hidden rows): the
	// filter options, `rows`, the three groups and the PR-branch fetch set all
	// derive from `sidebarSessions`.
	const sidebarSessions = $derived(
		$sessions.filter((s) => isVisibleSidebarSession(s, liveKeys))
	);

	// User-wide run and task updates own sidebar state, not transcript replay.
	// A cached turn/task can be out of date when a session is selected. Only a
	// local send bridges the wait for the server's run-start broadcast.
	const activeStreamingId = $derived($activeSession.optimisticStreaming ? $activeSession.sessionId : null);
	// The open session's own prompts and prose question (spec §5). The answer
	// object is reference-stable, so a streaming fold that changes none of it
	// leaves the rows be.
	const groupToolCalls = $derived(chatPreferences.current?.groupToolCalls !== false);
	const openNeedsNow = $derived(openSessionNeeds(
		$activeSession,
		groupToolCalls,
		!!activeId && $proseQuestionSessions.has(activeId),
		isTranscriptProseDismissed($proseQuestionDismissals, activeId, $activeSession.messages)
	));
	// Null while the open session waits on nothing, so switching sessions does
	// not re-derive every row.
	const openNeedsId = $derived(
		openNeedsNow.permissions > 0 || openNeedsNow.questions > 0 || openNeedsNow.proseQuestion ? activeId : null
	);
	// Prose questions remembered for sessions that are not open; the open one
	// reads its own transcript instead.
	const rememberedProse = $derived(withoutSession($proseQuestionSessions, activeId));
	const streamingMap = $derived($streamingSessions);

	function isStreaming(id: string): boolean {
		return streamingMap.has(id) || activeStreamingId === id;
	}

	function projectLabel(s: Session): string {
		if (s.project_name) return s.project_name;
		if (s.project_id) return projectsById.get(s.project_id)?.name || 'No project';
		return 'No project';
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

	interface FilterOption { id: string; label: string }

	const projectOptions = $derived.by((): FilterOption[] => {
		const map = new Map<string, string>();
		for (const s of sidebarSessions) {
			const key = projectKey(s);
			if (!map.has(key)) map.set(key, key === UNGROUPED_PROJECT ? 'No project' : projectLabel(s));
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
						optimisticStreaming: activeStreamingId === s.id,
						backgroundWork: !!s.has_running_tasks,
						parked: !!s.parked_task_kind,
						attention: $backgroundPermissions.get(s.id) ?? null,
						privateAccess: ($privateAccessPending.get(s.id) ?? 0) > 0,
						prState: sessionPrState(s),
						openPr: !s.pr_state && !!(branch && prSet?.has(branch)),
						isActive: !!s.is_active,
						failed: lastWorkFailed(s),
						open: sessionNeedsInput(
							s.id === openNeedsId,
							openNeedsNow,
							groupToolCalls && rememberedProse.has(s.id)
						)
					}),
					ts: sessionTimestamp(s)
				};
			})
			.sort((a, b) => b.ts - a.ts)
	);

	// "Needs you" lifts every ACTIVE row parked on a prompt out of Active, so an
	// active row moves when it starts or stops waiting on the user — never on
	// unrelated activity. Settling is the user saying "not
	// now" (#2324): a settled row always lands in Settled, keeping its amber badge
	// if the server still reports a wait.
	const laneOf = (r: CardRow) => sidebarGroup(r.status, !!r.session.is_active);
	const needsYouRows = $derived(rows.filter((r) => laneOf(r) === 'needs'));
	const activeRows = $derived(rows.filter((r) => laneOf(r) === 'active'));
	const settledRows = $derived(rows.filter((r) => laneOf(r) === 'settled'));
	let settledLimit = $state(SETTLED_LIMIT);
	const visibleSettledRows = $derived(settledRows.slice(0, settledLimit));
	const hiddenSettled = $derived(Math.max(0, settledRows.length - visibleSettledRows.length));
	// Only offer "Show fewer" when collapsing would actually hide something.
	const fullyExpanded = $derived(
		hiddenSettled === 0 && settledLimit > SETTLED_LIMIT && settledRows.length > SETTLED_LIMIT
	);
	// One older-sessions load at a time, covering every page a click fetches,
	// so the shelf reads "Loading" for the whole run instead of flickering.
	let loadingOlder = $state(false);
	let shelfSettledAt = 0;

	// Archived chats are browsed in Settings, not in the session sidebar.

	// Each lane names its own nothing-here state; "No sessions yet" under a
	// voice-only list reads as though the whole app were empty.
	const emptyLabel = 'No sessions yet';
	// An empty Settled lane says nothing the Active lane does not. It stays only
	// while older chats are one click away, because its shelf is how you get them.
	const settledLaneShown = $derived(settledRows.length > 0 || loadingOlder || $sessionsHasMore);
	onMount(() => {
		if (!isAdmin) return;
		// Agent names and avatars for the new-session agent card.
		void refreshAgentIdentities().catch(() => {});
		// The worker's answer is the only terminal gate (see terminal/gating.ts).
		void terminals.ensureCapability();
	});
	// Terminal page (a shell with no chat, for CLI sign-ins and setup): only
	// for a viewer the terminal routes admit, on a worker that has terminals.
	const terminalReady = $derived(isAdmin && terminalAccess(terminals.capability).kind === 'ready');

	// Keep the ticker in step with what's actually on screen. The prompt-parked
	// states keep their live timer too — a session blocked on you is still live.
	$effect(() => {
		retick([...needsYouRows, ...activeRows].some((row) => LIVE_TICK_STATUSES.has(row.status)));
	});

	// --- rail ----------------------------------------------------------------
	const BUILD_MEMBERS: ViewType[] = ['build', 'profile'];
	const SETTINGS_MEMBERS: ViewType[] = ['plugins', 'mcp', 'hooks'];
	const currentType = $derived($activeView?.type ?? null);
	const phone = $derived(platform.isMobile);

	// Admin gating stays HERE, above the preference layer: the rail and the
	// Customize dialog only ever see the destinations this role may open, so a
	// stored order can never surface an admin page to a member, and an admin's
	// order survives their role lapsing, because nothing rewrote it.
	const destinations = $derived<WorkspaceNavItem[]>([
		{ id: 'file-browser', label: 'Files', icon: Files, on: currentType === 'file-browser' },
		// A notebook, not a second sheet of paper: this sits right under Files.
		{ id: 'notes', label: 'Notes', icon: StickyNote, on: currentType === 'notes' },
		{ id: 'build', label: 'Agents', icon: Bot, on: currentType ? BUILD_MEMBERS.includes(currentType) : false },
		...(isAdmin
			? [{ id: 'automations' as const, label: 'Automations', icon: CalendarClock, on: currentType === 'automations' }]
			: []),
		{ id: 'project', label: 'Projects', icon: Folder, on: currentType === 'project' },
		{ id: 'prs', label: 'Pull requests', icon: GitPullRequest, on: currentType === 'prs' },
		{ id: 'stats', label: 'Stats', icon: BarChart3, on: currentType === 'stats' },
		{ id: 'docs', label: 'Docs', icon: BookOpen, on: currentType === 'docs' },
		...(terminalReady
			? [{ id: 'terminal' as const, label: 'Terminal', icon: SquareTerminal, on: currentType === 'terminal' }]
			: [])
	]);
	/** Settings is pinned at the rail's foot, never under More. */
	const settingsItem = $derived<WorkspaceNavItem | null>(
		isAdmin
			? { id: 'settings', label: 'Settings', icon: Settings, on: currentType === 'settings' || SETTINGS_MEMBERS.includes(currentType as ViewType) }
			: null
	);
	const split = $derived(splitWorkspaceItems(destinations, $workspacePrefs));
	// The phone's tab row holds four places plus More, so it has fewer seats than
	// the rail. Until someone reorders their list, the seats go by real use: the
	// chat list comes first in the rail itself, then Projects and Agents. A list
	// the user has reordered keeps their order; nothing but seats change.
	const phoneTabs = $derived.by<WorkspaceNavItem[]>(() => {
		const seats = settingsItem ? 2 : 3;
		const reordered = $workspacePrefs.order.join() !== WORKSPACE_ITEM_IDS.join();
		const rank = (item: WorkspaceNavItem) => {
			const at = PHONE_TAB_ORDER.indexOf(item.id);
			return at < 0 ? PHONE_TAB_ORDER.length : at;
		};
		const pool = reordered ? split.shown : [...split.shown].sort((a, b) => rank(a) - rank(b));
		return pool.slice(0, seats);
	});
	const railItems = $derived(phone ? phoneTabs : split.shown);
	const moreItems = $derived(
		phone ? destinations.filter((item) => !phoneTabs.some((tab) => tab.id === item.id)) : split.hidden
	);
	const moreOn = $derived(moreItems.some((item) => item.on));
	/** The chat list, and the chat or board it leads to, are what is on screen. */
	const chatsOn = $derived(currentType === null || currentType === 'session' || currentType === 'board');
	let listRef = $state<HTMLElement | null>(null);

	function openChats() {
		sheet = null;
		// Already here: on the phone the list is the page, so go to its top.
		if (chatsOn) listRef?.scrollTo({ top: 0, behavior: 'smooth' });
		else (onBackToChats ?? backToChats)();
	}
	// One kind of session; the project is picked (or not) on the new screen.
	const newLabel = 'New chat';

	/** Which overlay is up: the More menu, the filter, or neither. */
	let sheet = $state<'more' | 'filter' | null>(null);
	let customizeOpen = $state(false);

	function openCustomize() {
		sheet = null;
		customizeOpen = true;
	}

	function toggleSheet(which: 'more' | 'filter') {
		sheet = sheet === which ? null : which;
	}

	// --- DesktopUpdateButton bridge (#1623) -----------------------------------
	// The update button owns the update state; More only offers the manual
	// check while the button says one makes sense.
	let updater = $state<{ checkForUpdates: () => Promise<void> } | null>(null);
	let canCheck = $state(false);

	function checkForUpdates() {
		sheet = null;
		void updater?.checkForUpdates();
	}

	// --- filters (sheet) -----------------------------------------------------
	const filtersSet = $derived(!!projectFilter || !!deviceFilter);

	// --- settled day labels --------------------------------------------------
	const DAY = 86_400_000;
	function dayLabel(ts: number, nowMs: number): string {
		const start = new Date(nowMs);
		start.setHours(0, 0, 0, 0);
		const today = start.getTime();
		if (ts >= today) return 'Today';
		if (ts >= today - DAY) return 'Yesterday';
		if (ts >= today - 6 * DAY) return 'Earlier this week';
		return 'Older';
	}
	// Inside a lane, rows sit under their project when the lane spans more than
	// one, so no row has to repeat it. A project's place follows its most recent
	// row, and rows keep their recency order inside it.
	function byProject(laneRows: CardRow[]): { key: string; label: string | null; rows: CardRow[] }[] {
		const groups = new Map<string, CardRow[]>();
		for (const row of laneRows) {
			const key = projectKey(row.session);
			const group = groups.get(key);
			if (group) group.push(row);
			else groups.set(key, [row]);
		}
		if (groups.size < 2) return [{ key: 'all', label: null, rows: laneRows }];
		return [...groups].map(([key, rows]) => ({
			key,
			label: key === UNGROUPED_PROJECT ? 'No project' : projectLabel(rows[0].session),
			rows
		}));
	}
	const needsYouGroups = $derived(byProject(needsYouRows));
	const activeGroups = $derived(byProject(activeRows));

	const settledDays = $derived.by(() => {
		const out: { label: string; rows: CardRow[] }[] = [];
		for (const row of visibleSettledRows) {
			const label = dayLabel(row.ts, now);
			const last = out[out.length - 1];
			if (last?.label === label) last.rows.push(row);
			else out.push({ label, rows: [row] });
		}
		return out;
	});

	// --- multi-select --------------------------------------------------------
	// A long press on a row starts selecting; tapping rows adds or removes
	// them; clearing the last one ends it.
	let selected = $state<Set<string>>(new Set());
	const selecting = $derived(selected.size > 0);
	const selectedRows = $derived(rows.filter((r) => selected.has(r.session.id)));
	const anySelectedActive = $derived(selectedRows.some((r) => r.session.is_active));
	const anySelectedSettled = $derived(selectedRows.some((r) => !r.session.is_active));

	// Rows that left the list (archived elsewhere, filtered out) leave the
	// selection too, so the count never names rows the user cannot see.
	$effect(() => {
		const present = new Set(rows.map((r) => r.session.id));
		const current = untrack(() => selected);
		if ([...current].some((id) => !present.has(id))) {
			selected = new Set([...current].filter((id) => present.has(id)));
		}
	});

	function toggleSelected(id: string) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function startSelecting(id: string) {
		navigator.vibrate?.(10);
		selected = new Set([...selected, id]);
	}

	function cancelSelection() {
		selected = new Set();
	}

	function reportBulkFailure(failed: number, total: number) {
		notify(
			failed === total ? 'Failed to update the sessions' : `Failed to update ${failed} of ${total} sessions`,
			{ severity: 'error' }
		);
	}

	function offerUndo(message: string, undo: () => Promise<unknown>) {
		const id = notify.banner(message, {
			severity: 'info',
			persist: false,
			duration_ms: 6000,
			action: {
				label: 'Undo',
				onClick: () => {
					notify.dismissLive(id);
					undo().catch(() => notify('Failed to undo', { severity: 'error' }));
				}
			}
		});
	}

	const plural = (n: number) => `${n} session${n === 1 ? '' : 's'}`;

	async function runBulk(kind: 'settle' | 'activate' | 'archive') {
		const targets = selectedRows.filter((r) =>
			kind === 'settle' ? r.session.is_active : kind === 'activate' ? !r.session.is_active : true
		);
		cancelSelection();
		if (targets.length === 0) return;
		const ids = targets.map((r) => r.session.id);
		const wasActive = new Map(targets.map((r) => [r.session.id, !!r.session.is_active]));
		const apply = (id: string) =>
			kind === 'archive'
				? appData.setSessionArchived(id, true, wasActive.get(id))
				: appData.setSessionActive(id, kind === 'activate');
		const results = await Promise.allSettled(ids.map(apply));
		const done = ids.filter((_, i) => results[i].status === 'fulfilled');
		const failed = ids.length - done.length;
		if (failed) reportBulkFailure(failed, ids.length);
		if (done.length === 0) return;
		if (kind === 'settle') {
			if (activeId && done.includes(activeId) && !phone) onSettledOpenSession();
		} else if (kind === 'archive') {
			offerUndo(`Archived ${plural(done.length)}`, () =>
				Promise.all(done.map((id) => appData.setSessionArchived(id, false, wasActive.get(id))))
			);
		}
	}

	// --- keyboard ------------------------------------------------------------
	function handleWindowKeydown(e: KeyboardEvent) {
		if (e.key !== 'Escape') return;
		// Innermost first: one Escape closes one layer.
		if (customizeOpen) customizeOpen = false;
		else if (sheet) sheet = null;
		else if (selecting) cancelSelection();
		else return;
		e.preventDefault();
		e.stopPropagation();
	}

	function pickFeature(type: ViewType) {
		sheet = null;
		onOpenFeature(type);
	}

	/**
	 * Pull older pages until one adds a settled row this list shows, or the
	 * server runs out, then reveal the new rows in place. The visible limit only
	 * grows once rows actually arrived; growing it up front (the old behaviour)
	 * turned an empty page into a "Show fewer" that collapsed nothing.
	 */
	async function loadMoreSessions() {
		if (loadingOlder) return;
		loadingOlder = true;
		const before = settledRows.length;
		try {
			for (let page = 0; page < OLDER_PAGES_PER_CLICK && $sessionsHasMore; page++) {
				await appData.loadMoreSessions();
				if (settledRows.length > before) break;
			}
			if (settledRows.length > before) {
				settledLimit = Math.max(settledLimit, before + SHOW_MORE_STEP);
			}
		} finally {
			loadingOlder = false;
			shelfSettledAt = Date.now();
		}
	}

	function showFewerSettled() {
		if (Date.now() - shelfSettledAt < SHELF_SETTLE_MS) return;
		settledLimit = SETTLED_LIMIT;
	}
</script>

<svelte:window onkeydown={handleWindowKeydown} />

{#snippet card(row: CardRow, compact = false)}
	<SessionCard
		session={row.session}
		status={row.status}
		openPr={row.openPr}
		projectLabel={projectLabel(row.session)}
		active={row.session.id === activeId}
		compacting={row.session.id === activeId && $activeSession.liveCompaction?.phase === 'running'}
		{now}
		timestamp={row.ts}
		{compact}
		{selecting}
		selected={selected.has(row.session.id)}
		onSelectToggle={toggleSelected}
		onLongPress={startSelecting}
		onOpen={onOpenSession}
		onSettled={onSettledOpenSession}
	/>
{/snippet}

{#snippet laneRows(groups: { key: string; label: string | null; rows: CardRow[] }[], compact = false)}
	{#each groups as group (group.key)}
		{#if group.label}
			<div class="proj-head" title={group.label}>{group.label}</div>
		{/if}
		{#each group.rows as row (row.session.id)}
			{@render card(row, compact)}
		{/each}
	{/each}
{/snippet}

{#snippet sect(key: 'needs' | 'active' | 'settled', label: string, count: number)}
	<button
		class="sect"
		class:needs={key === 'needs'}
		class:closed={collapsedGroups[key]}
		onclick={() => toggleGroup(key)}
		aria-expanded={!collapsedGroups[key]}
	>
		<span class="lbl">{label}</span> <span class="n">{count}</span>
		<span class="chev"><ChevronDown size={14} strokeWidth={2} /></span>
	</button>
{/snippet}

<aside class="sidebar" class:phone aria-label="Chats">
	<SidebarRail
		items={railItems}
		settings={settingsItem}
		{newLabel}
		moreOpen={sheet === 'more'}
		{moreOn}
		{phone}
		onNew={() => { sheet = null; cancelSelection(); onNewSession(); }}
		onPick={pickFeature}
		onPreload={preloadPanelForView}
		onMore={() => toggleSheet('more')}
		onCustomize={openCustomize}
		{chatsOn}
		onChats={openChats}
	/>

	<div class="col" class:selecting class:rail-hidden={railOnly}>
		<!-- Selection bar: slides over the header while rows are selected. -->
		<div class="selbar" inert={!selecting} aria-hidden={selecting ? undefined : 'true'}>
			<button class="icon-btn" type="button" aria-label="Cancel selection" onclick={cancelSelection}>
				<X size={18} strokeWidth={1.75} />
			</button>
			<span class="cnt" aria-live="polite">{selected.size} selected</span>
			{#if anySelectedSettled}
				<button class="act" type="button" onclick={() => runBulk('activate')}>
					<Undo2 size={15} strokeWidth={2} />Active
				</button>
			{/if}
			<button class="act" type="button" aria-label="Archive" title="Archive" onclick={() => runBulk('archive')}>
				<Archive size={15} strokeWidth={2} />
			</button>
			{#if anySelectedActive}
				<button class="act primary" type="button" onclick={() => runBulk('settle')}>
					<Check size={15} strokeWidth={2.25} />Settle
				</button>
			{/if}
		</div>

		<div class="p-top">
			<header class="p-head">
				<span class="p-title">Chats</span>
				<DesktopUpdateButton bind:this={updater} bind:canCheck />
				<!-- The bell's only home: it used to sit in both top bars, which meant
				     two copies on mobile and none while the drawer was open. -->
				<NotificationCenter />
				{#if phone}
					<button class="icon-btn p-new" type="button" aria-label={newLabel} onclick={() => { sheet = null; cancelSelection(); onNewSession(); }}>
						<SquarePen size={20} strokeWidth={1.75} />
					</button>
				{/if}
				{#if headerAction}
					{@render headerAction()}
				{/if}
			</header>

			<div class="searchrow">
				<button class="search" type="button" onclick={() => onOpenSpotlight?.()} disabled={!onOpenSpotlight}>
					<Search size={15} strokeWidth={1.75} />
					<span>Search</span>
					{#if !phone}<kbd>Ctrl K</kbd>{/if}
				</button>
				<button
					class="filter-btn"
					class:set={filtersSet}
					class:open={sheet === 'filter'}
					type="button"
					aria-label="Filter"
					aria-haspopup="dialog"
					aria-expanded={sheet === 'filter'}
					onclick={() => toggleSheet('filter')}
				>
					<ListFilter size={17} strokeWidth={1.75} />
				</button>
			</div>

			{#if filtersSet}
				<div class="chipline">
					{#if projectFilter}
						<button class="chip" type="button" aria-label={`Clear project filter ${projectFilterLabel}`} onclick={() => (projectFilter = null)}>
							<span>{projectFilterLabel}</span><X size={13} strokeWidth={2} />
						</button>
					{/if}
					{#if deviceFilter}
						<button class="chip" type="button" aria-label={`Clear device filter ${deviceFilterLabel}`} onclick={() => (deviceFilter = null)}>
							<span>{deviceFilterLabel}</span><X size={13} strokeWidth={2} />
						</button>
					{/if}
				</div>
			{/if}

		</div>

		<div class="list" class:edge-guard={phone} bind:this={listRef}>
			{#if needsYouRows.length > 0}
				{@render sect('needs', 'Needs you', needsYouRows.length)}
				{#if !collapsedGroups.needs}
					<div class="cards" data-group="needs-you">
						{@render laneRows(needsYouGroups)}
					</div>
				{/if}
			{/if}

			{@render sect('active', 'Active', activeRows.length)}
			{#if !collapsedGroups.active}
				{#if activeRows.length === 0}
					<p class="empty">{settledLaneShown ? 'Nothing active. Swipe a settled session left to bring it back.' : emptyLabel}</p>
				{:else}
					<div class="cards" data-group="active">
						{@render laneRows(activeGroups)}
					</div>
				{/if}
			{/if}

			{#if settledLaneShown}
				{@render sect('settled', 'Settled', settledRows.length)}
			{/if}
			{#if settledLaneShown && !collapsedGroups.settled}
				{#each settledDays as day (day.label)}
					<div class="daylabel">{day.label}</div>
					<div class="cards" data-group="settled">
						{@render laneRows(byProject(day.rows), true)}
					</div>
				{/each}

				{#if loadingOlder}
					<!-- Held for the whole multi-page run so the label cannot flip
					     between "Load older", "Show N more" and "Show fewer" mid-load. -->
					<button class="shelf" disabled aria-busy="true">
						<Loader2 class="shelf-spin" size={14} strokeWidth={2} />
						Loading older sessions…
					</button>
				{:else if hiddenSettled > 0}
					<button class="shelf" onclick={() => (settledLimit += SHOW_MORE_STEP)}>
						Show {Math.min(hiddenSettled, SHOW_MORE_STEP)} more
						<ChevronDown size={14} strokeWidth={2} />
					</button>
				{:else if $sessionsHasMore}
					<!-- Everything loaded is on screen and the server holds more: the
					     initial load is capped at one page (W1), so fetch the next one
					     on demand instead of paging through the whole history up front.
					     `has_more` already excludes archived chats (server-side). -->
					<button class="shelf" onclick={loadMoreSessions}>
						Load older sessions
						<ChevronDown size={14} strokeWidth={2} />
					</button>
				{:else if fullyExpanded}
					<button class="shelf" onclick={showFewerSettled}>
						Show fewer
						<ChevronRight size={14} strokeWidth={2} />
					</button>
				{/if}
			{/if}
		</div>

		<SidebarSheet open={sheet === 'filter'} label="Filter chats" {phone} placement="popover" onClose={() => (sheet = null)}>
			{#if projectOptions.length > 0}
				<h4>Project</h4>
				<div class="opts">
					<button class="opt" type="button" aria-pressed={!projectFilter} onclick={() => (projectFilter = null)}>All projects</button>
					{#each projectOptions as opt (opt.id)}
						<button class="opt" type="button" aria-pressed={projectFilter === opt.id} onclick={() => (projectFilter = opt.id)}>{opt.label}</button>
					{/each}
				</div>
			{/if}
			<h4>Device</h4>
			<div class="opts">
				<button class="opt" type="button" aria-pressed={!deviceFilter} onclick={() => (deviceFilter = null)}>All devices</button>
				{#each deviceOptions as opt (opt.id)}
					<button class="opt" type="button" aria-pressed={deviceFilter === opt.id} onclick={() => (deviceFilter = opt.id)}>{opt.label}</button>
				{/each}
			</div>
		</SidebarSheet>

		<SidebarSheet open={sheet === 'more'} label="More destinations" role="menu" {phone} placement="flyout" onClose={() => (sheet = null)}>
			{#if phone}<h4>More</h4>{/if}
			{#if moreItems.length > 0}
				<div class="wgrid">
					{#each moreItems as item (item.id)}
						<button
							class="tile"
							class:on={item.on}
							type="button"
							role="menuitem"
							aria-current={item.on ? 'page' : undefined}
							onclick={() => pickFeature(item.id)}
							onpointerenter={() => preloadPanelForView(item.id)}
							onfocus={() => preloadPanelForView(item.id)}
						>
							<item.icon size={phone ? 20 : 15} strokeWidth={1.75} />
							<span>{item.label}</span>
						</button>
					{/each}
				</div>
			{:else}
				<p class="wempty">Everything is on the rail.</p>
			{/if}
			<button class="custrow" type="button" role="menuitem" onclick={openCustomize}>
				<SlidersHorizontal size={phone ? 17 : 15} strokeWidth={1.75} />
				<span>Customize the rail</span>
				{#if !phone}<small>{split.shown.length} on the rail</small>{/if}
			</button>
			{#if isAdmin && canCheck}
				<button class="custrow" type="button" role="menuitem" onclick={checkForUpdates}>
					<RefreshCw size={phone ? 17 : 15} strokeWidth={1.75} />
					<span>Check for updates</span>
				</button>
			{/if}
		</SidebarSheet>
	</div>

	{#if customizeOpen}
		<WorkspaceCustomize items={destinations} showSettings={isAdmin} {phone} onClose={() => (customizeOpen = false)} />
	{/if}
</aside>

<style>
	.sidebar {
		position: relative;
		width: 100%;
		height: 100%;
		display: flex;
		min-height: 0;
		background: var(--sidebar);
		color: var(--sidebar-foreground);
	}

	.sidebar.phone { flex-direction: column-reverse; }

	/* Hidden, not removed: coming back to chats stays instant. */
	.col.rail-hidden { display: none; }

	.col {
		position: relative;
		flex: 1;
		min-width: 0;
		min-height: 0;
		display: flex;
		flex-direction: column;
		/* clip, not hidden: a hidden box can still be scrolled, and focus landing
		   in the sliding selection bar used to shift the whole column up. */
		overflow: hidden;
		overflow: clip;
	}

	/* Top block: mode toggle and header buttons, search and filter chips.
	   No z-index on the header itself (#1678): it would trap the
	   notification panel below the rows. */
	.p-top { flex: none; position: relative; padding: 0 8px; background: var(--sidebar); }
	.phone .p-top { padding-top: env(safe-area-inset-top, 0px); }
	.p-head { display: flex; align-items: center; gap: 2px; height: 52px; }
	.p-title { margin-right: auto; padding-left: 6px; font-size: 16px; font-weight: 600; letter-spacing: -0.005em; color: var(--sidebar-foreground); }

	/* The bell is shared with the top bars, where it is right-anchored under a
	   wide header. Inside the panel that anchor puts most of it off the left
	   edge, so pin it to the viewport under the header instead. */
	.p-head :global(.notif-panel) {
		position: fixed;
		top: 52px;
		left: 8px;
		right: auto;
		width: min(340px, calc(100vw - 16px));
		max-height: min(440px, calc(100vh - 140px));
	}

	.icon-btn {
		width: 40px;
		height: 40px;
		flex: none;
		display: grid;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: 10px;
		background: transparent;
		color: var(--sidebar-muted);
		cursor: pointer;
	}
	.icon-btn:hover { background: var(--sidebar-accent); color: var(--foreground); }
	.icon-btn.p-new { width: 44px; height: 44px; color: var(--sidebar-foreground); }

	.searchrow { display: flex; gap: 6px; padding-bottom: 8px; }
	.search {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 9px;
		height: 32px;
		padding: 0 10px;
		border: 1px solid var(--field-border);
		border-radius: var(--radius-control);
		background: var(--field-bg);
		color: var(--sidebar-muted);
		font: inherit;
		font-size: 13px;
		text-align: left;
		cursor: pointer;
		transition: border-color 0.13s ease, color 0.13s ease;
	}
	.search:hover:not(:disabled) { color: var(--sidebar-foreground); }
	.search:disabled { cursor: default; }
	.search :global(svg) { flex: none; }
	/* No keyboard shortcut to advertise at phone width. */
	@media (max-width: 480px) {
		.search kbd { display: none; }
	}
	.search kbd {
		margin-left: auto;
		font-family: inherit;
		font-size: var(--fs-caption);
		color: var(--subtle-foreground);
	}
	.phone .search { height: 44px; font-size: 16px; }

	.filter-btn {
		position: relative;
		width: 32px;
		height: 32px;
		flex: none;
		display: grid;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: var(--radius-control);
		background: transparent;
		color: var(--sidebar-muted);
		cursor: pointer;
	}
	.phone .filter-btn { width: 44px; height: 44px; }
	.filter-btn:hover { background: var(--row-hover); color: var(--sidebar-foreground); }
	.filter-btn.open { background: var(--row-selected); color: var(--sidebar-foreground); }
	.filter-btn.set { color: var(--primary); }
	.filter-btn.set::after {
		content: '';
		position: absolute;
		top: 6px;
		right: 6px;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--primary);
	}

	.chipline { display: flex; flex-wrap: wrap; gap: 6px; padding-bottom: 8px; }
	.chip {
		max-width: 100%;
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 28px;
		padding: 0 6px 0 10px;
		border: 0;
		border-radius: 14px;
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		color: var(--primary);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.chip span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.chip :global(svg) { flex: none; }

	/* The ledger */
	.list {
		position: relative;
		flex: 1 1 auto;
		min-height: 0;
		overflow-x: hidden;
		overflow-y: auto;
		padding: 0 8px 12px;
		scrollbar-width: thin;
		scrollbar-color: var(--border-strong) transparent;
	}
	.phone .list { padding-bottom: 24px; }
	/* The mobile drawer is the full screen wide, and Android reserves the
	   outer ~24dp of each edge for its back gesture. A swipe rail flush with
	   that edge got taken for a back swipe, and taps on its outer half went
	   to the system. Hold the rows off the right edge so the whole row, and
	   the place a thumb starts a left (settle) swipe, sit inside the app's own
	   touch area. The left edge needs no guard: the icon rail already keeps
	   the rows, and the start of a right (archive) swipe, clear of it. */
	.list.edge-guard { padding-right: 24px; }
	.cards { display: flex; flex-direction: column; gap: 2px; }
	/* Tiers: the lane label, then this quieter project label, then its rows. */
	.proj-head {
		padding: 16px 8px 4px 12px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--fs-caption);
		font-weight: 500;
		color: var(--subtle-foreground);
	}
	.proj-head:first-child { padding-top: 4px; }

	.sect {
		position: sticky;
		top: 0;
		z-index: 6;
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		padding: 12px 8px 6px 12px;
		border: 0;
		background: linear-gradient(var(--sidebar) 78%, transparent);
		color: var(--muted-foreground);
		font: inherit;
		font-size: var(--fs-label);
		font-weight: 500;
		text-align: left;
		cursor: pointer;
	}
	.sect .lbl { flex: none; }
	.sect:hover { color: var(--sidebar-foreground); }
	.sect:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; border-radius: var(--radius-control); }
	.sect .n {
		font-size: var(--fs-caption);
		font-weight: 400;
		font-variant-numeric: tabular-nums;
		color: var(--subtle-foreground);
	}
	.sect .chev { margin-left: auto; display: inline-flex; color: var(--subtle-foreground); transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1); }
	.sect.closed .chev { transform: rotate(-90deg); }
	.sect.needs .lbl { color: var(--warning); }

	.daylabel { padding: 10px 8px 3px; font-size: var(--fs-caption); font-weight: 500; color: var(--subtle-foreground); }
	.empty {
		margin: 0;
		padding: 14px 16px 18px;
		font-size: 12.5px;
		line-height: 1.5;
		text-align: center;
		color: var(--subtle-foreground);
	}

	.shelf {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		width: 100%;
		height: 36px;
		margin-top: 10px;
		border: 0;
		border-radius: 10px;
		background: transparent;
		color: var(--subtle-foreground);
		font: inherit;
		font-size: 12.5px;
		cursor: pointer;
	}
	.shelf:hover:not(:disabled) { background: var(--sidebar-accent); color: var(--muted-foreground); }
	.shelf:disabled { cursor: default; opacity: 0.7; }

	/* Selection bar */
	.selbar {
		position: absolute;
		left: 0;
		right: 0;
		top: 0;
		z-index: 8;
		display: flex;
		align-items: center;
		gap: 6px;
		height: 52px;
		padding: 0 8px;
		background: color-mix(in oklab, var(--primary) 12%, var(--sidebar));
		border-bottom: 1px solid color-mix(in oklab, var(--primary) 30%, var(--sidebar-border));
		transform: translateY(-100%);
		transition: transform 0.26s cubic-bezier(0.16, 1, 0.3, 1);
	}
	.phone .selbar { height: calc(56px + env(safe-area-inset-top, 0px)); padding-top: env(safe-area-inset-top, 0px); }
	.col.selecting .selbar { transform: none; }
	.cnt { margin-right: auto; padding-left: 4px; font-size: 14px; font-weight: 650; color: var(--foreground); }
	.act {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 34px;
		padding: 0 12px;
		border: 0;
		border-radius: 10px;
		background: var(--row-hover);
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		font-weight: 600;
		cursor: pointer;
	}
	.act:hover { background: var(--row-selected); }
	.act.primary { background: var(--action); color: var(--action-foreground); }
	.act.primary:hover { background: var(--action-hover); }

	/* Sheet contents (SidebarSheet supplies the surface). */
	h4 { margin: 0 4px 8px; font-size: 13px; font-weight: 600; color: var(--muted-foreground); }
	.opts { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
	.opt {
		max-width: 100%;
		height: 30px;
		padding: 0 11px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		border: 1px solid var(--border);
		border-radius: 15px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12.5px;
		cursor: pointer;
	}
	.phone .opt { height: 36px; padding: 0 13px; border-radius: 18px; font-size: 13.5px; }
	.opt:hover { color: var(--foreground); border-color: var(--border-strong); }
	.opt[aria-pressed='true'] {
		background: color-mix(in oklch, var(--primary) 16%, transparent);
		border-color: color-mix(in oklch, var(--primary) 45%, transparent);
		color: var(--primary);
	}

	/* More: a tile grid on the phone, a plain menu on desktop. */
	.wgrid { display: flex; flex-direction: column; gap: 1px; }
	.tile {
		display: flex;
		align-items: center;
		gap: 10px;
		height: 32px;
		padding: 0 8px;
		border: 0;
		border-radius: 7px;
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		text-align: left;
		cursor: pointer;
	}
	.tile :global(svg) { flex: none; color: var(--muted-foreground); }
	.tile span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.tile:hover,
	.tile.on { background: var(--sidebar-accent-active); }
	/* A plain labelled list, the same rows as the two beneath it. */
	.phone .tile {
		height: 48px;
		gap: 12px;
		padding: 0 10px;
		border-radius: 10px;
		font-size: 14px;
	}
	.phone .tile.on { font-weight: 600; }
	.wempty { margin: 0; padding: 6px 8px 4px; font-size: 12.5px; color: var(--muted-foreground); }

	.custrow {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		height: 32px;
		margin-top: 5px;
		padding: 0 8px;
		border: 0;
		border-top: 1px solid var(--border);
		border-radius: 0 0 7px 7px;
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		white-space: nowrap;
		cursor: pointer;
	}
	.custrow + .custrow { margin-top: 0; border-top: 0; border-radius: 7px; }
	.custrow:hover { background: var(--sidebar-accent-active); }
	.custrow :global(svg) { flex: none; color: var(--muted-foreground); }
	.custrow small { margin-left: auto; font-size: 11.5px; color: var(--subtle-foreground); }
	.phone .custrow {
		height: 48px;
		margin-top: 10px;
		gap: 12px;
		padding: 0 10px;
		border: 0;
		border-radius: 12px;
		box-shadow: inset 0 0 0 1px var(--sidebar-border);
		font-size: 14px;
	}
	.phone .custrow + .custrow { margin-top: 6px; }

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	.shelf :global(.shelf-spin) { animation: shelf-spin 0.9s linear infinite; }
	@keyframes shelf-spin { to { transform: rotate(360deg); } }

	/* Touch sizing on any touch-only pointer, not just the mobile shell. */
	@media (hover: none) {
		.search { height: 40px; }
		.filter-btn { width: 40px; height: 40px; }
		.opt { height: 36px; }
		.shelf { height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.selbar, .sect .chev, .search { transition: none; }
		.shelf :global(.shelf-spin) { animation: none; }
	}
</style>
