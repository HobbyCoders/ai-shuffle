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
	import { onDestroy } from 'svelte';
	import { Plus, X, CircleDot, Undo2, FolderClosed, MessageSquare, ChevronRight, ChevronDown, GitBranch, GitFork, Play, Monitor, MonitorOff, Link2, Unlink, Check, CalendarClock, Trash2, Pencil } from 'lucide-svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import { browser } from '$app/environment';
	import { appData, sessions, projects } from '$lib/stores/app-data.svelte';
	import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
	import { forkSession, adoptSession, api } from '$lib/api/client';
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
	// Users with many sessions can fold away the Active / Recent / Projects
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
	// Armed-confirm UI state ONLY: which row is showing its Confirm/Cancel pair.
	// Freely nulled by Escape / outside-click / Cancel / re-arming another row —
	// never use it to ask "is a delete request running" (that's deleteInFlightId).
	let deletingSessionId = $state<string | null>(null);
	// In-flight delete guard ONLY: the id whose DELETE request is currently
	// awaiting. Set right before the request and cleared in its `finally`; read by
	// `openSession` so a stray row click can't open the mid-delete session (which
	// the top bar would surface as a brand-new empty chat). Nothing else writes it.
	let deleteInFlightId = $state<string | null>(null);
	let deleteLoading = $state(false);
	// Safety auto-reset for the delete confirmation: if the user arms "Clear" but
	// walks away, drop the confirm state after a while so a stale confirm can't
	// fire later. The timer id is kept so we can cancel it whenever the confirm is
	// resolved or replaced — a fire-and-forget timer used to blank the confirm out
	// from under the user (it looked like the button vanished on its own).
	let deleteConfirmTimer: ReturnType<typeof setTimeout> | null = null;
	// Mobile only: which row has its action rail expanded out of the ⋮ menu.
	// Single value = one open at a time; desktop keeps the always-inline rail.
	let actionsOpenId = $state<string | null>(null);
	// Inline rename UI state: the row whose title is currently swapped for an
	// edit input (null = none), plus the live editor value. One row at a time.
	let renamingSessionId = $state<string | null>(null);
	let renameValue = $state('');

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
	// this so the filter cuts across active/recent/projects uniformly.
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
			if (!!a.is_active !== !!b.is_active) return a.is_active ? -1 : 1;
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
			// via the server-resolved origin (v2 projects); fall back to the link
			// group, then the raw project id. origin is 1:1 with link_group_id for
			// grouped projects, so the fold is identical with or without it present
			// (no regroup flash across the cutover).
			const key = s.origin || s.link_group_id || s.project_id || UNGROUPED;
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

	// Active sessions (the ones the user is currently working in) get their own
	// section so they always stay visible at the top instead of competing with
	// everything else for the limited Recent slots. Recent therefore excludes
	// active sessions.
	const active = $derived(visibleSessions.filter((s) => s.is_active).sort((a, b) => tsOf(b) - tsOf(a)));
	const recent = $derived(
		visibleSessions.filter((s) => !s.is_active).sort((a, b) => tsOf(b) - tsOf(a)).slice(0, RECENT_LIMIT)
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
		// The ⋮ toggle and rail buttons all stopPropagation, so any click that
		// reaches the window is outside an open rail — collapse it.
		if (actionsOpenId !== null) {
			actionsOpenId = null;
			disarmDeleteConfirm();
		}
	}
	function handleFilterWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			deviceMenuOpen = false;
			linkMenuOpen = false;
			actionsOpenId = null;
			disarmDeleteConfirm();
		}
	}

	// Flip a session's Active membership: an active row is marked complete
	// (is_active=false → it drops into Recent), an inactive row is moved back to
	// Active. State-based so it also reads correctly in an opened project's list
	// where active and inactive rows mix.
	async function handleToggleActive(e: MouseEvent | KeyboardEvent, s: Session) {
		e.stopPropagation();
		actionsOpenId = null;
		try {
			// Optimistic in-place update (re-groups Active/Recent without a full
			// paginated refetch); the store rolls back on error.
			await appData.setSessionActive(s.id, !s.is_active);
		} catch {
			toast.push('Failed to update session', { tone: 'error' });
		}
	}

	// Mobile: the ⋮ toggle expands/collapses a row's action rail. Collapsing also
	// clears any pending delete confirmation so it doesn't linger while hidden.
	function toggleRowActions(e: MouseEvent | KeyboardEvent, id: string) {
		e.stopPropagation();
		if (actionsOpenId === id) {
			actionsOpenId = null;
			disarmDeleteConfirm();
		} else {
			actionsOpenId = id;
		}
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
		actionsOpenId = null;
		await resumeHere(s);
	}
	async function handleFork(e: MouseEvent | KeyboardEvent, s: Session) {
		e.stopPropagation();
		actionsOpenId = null;
		await forkAndOpen(s);
	}
	// "Resume here" adopts a foreign-instance session to this install and opens
	// it. It is only ever rendered for remote (cross-install) rows — a local row
	// has nothing to adopt and the row's own click already opens it, so the
	// button would be a redundant "Open" (see the `session.is_remote` guard
	// around its markup below).
	function resumeTitle(s: Session): string {
		return s.device_name ? `Resume here (${s.device_name})` : 'Resume here';
	}

	// "Resume here": adopt a foreign-instance session to this install, reload so
	// worktree/ownership fields are fresh, then open. Worktree sessions rebind via
	// the existing recreate path.
	async function resumeHere(s: Session) {
		try {
			const res = await adoptSession(s.id);
			await appData.loadSessions();
			// Non-blocking cross-device advisory (e.g. an unpushed worktree
			// branch): surface it before opening so the user knows the send
			// may fail until they push, but still let them in.
			if (res?.warning) {
				toast.push(res.warning, { tone: 'warning' });
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

	// Arm the delete confirmation on a row and (re)start its safety auto-reset.
	// Cancels any prior timer first so re-arming on another row can't leave a
	// stale timer that later blanks the new confirm.
	function armDeleteConfirm(id: string) {
		disarmDeleteConfirm();
		deletingSessionId = id;
		deleteConfirmTimer = setTimeout(() => {
			if (deletingSessionId === id) deletingSessionId = null;
			deleteConfirmTimer = null;
		}, 8000);
	}
	// Clear the confirm state and cancel its pending auto-reset timer. Use this
	// anywhere `deletingSessionId` is dropped so no orphaned timer survives.
	function disarmDeleteConfirm() {
		if (deleteConfirmTimer !== null) {
			clearTimeout(deleteConfirmTimer);
			deleteConfirmTimer = null;
		}
		deletingSessionId = null;
	}
	// The 8s auto-reset timer above outlives the component unless cancelled: if
	// the board unmounts while a confirm is armed (e.g. the user navigates away),
	// the timer would otherwise fire post-unmount and write to `deletingSessionId`.
	onDestroy(() => {
		disarmDeleteConfirm();
	});

	async function handleDelete(e: MouseEvent | KeyboardEvent, id: string) {
		e.stopPropagation();
		if (deletingSessionId === id) {
			// Confirmed — drop the armed-confirm UI (and its auto-reset timer) and
			// hand the id over to the dedicated in-flight guard. The guard must NOT
			// reuse deletingSessionId: Escape, click-outside, Cancel and arming
			// another row all null that UI state at will, and while the delete is in
			// flight the disabled Confirm button retargets clicks to the parent row —
			// openSession needs a guard nobody else can tear down mid-request.
			disarmDeleteConfirm();
			actionsOpenId = null;
			deleteInFlightId = id;
			deleteLoading = true;
			try {
				// "Delete session" removes the session for good — deleteSession splices
				// the row out of the board optimistically (rolling back on error) and
				// the backend hard-deletes the transcript + artifacts. Crucially we stay
				// ON the sessions board: we never call onNewSession()/onOpenSession(), so
				// nothing navigates the user away. If the deleted session happens to be
				// the one open in the chat tab, reset THAT tab to a fresh New Chat via
				// activeSession.newChat() (state-only; unlike handleNewSession() it does
				// NOT flip the view) so the background chat isn't left pointing at a
				// now-gone session — the board itself remains on screen throughout.
				// (In-place clear of a conversation still lives on the chat view.)
				await appData.deleteSession(id);
				if (activeId === id) activeSession.newChat();
			} catch {
				// DELETE has no streaming 409 guard (the backend hard-deletes
				// regardless), so any failure is a plain error — deleteSession has
				// already rolled the optimistic splice back and resynced the list.
				toast.push('Failed to delete session', { tone: 'error' });
			} finally {
				deleteLoading = false;
				deleteInFlightId = null;
			}
		} else {
			armDeleteConfirm(id);
		}
	}
	function cancelDelete(e: MouseEvent | KeyboardEvent) {
		e.stopPropagation();
		actionsOpenId = null;
		disarmDeleteConfirm();
	}
	// --- Inline rename -----------------------------------------------------
	// Focus (and select-all) the rename input the moment it mounts, so the user
	// can immediately overtype the current title. A Svelte action fires on the
	// freshly-created node, so no manual tick/setTimeout is needed.
	function focusSelect(node: HTMLInputElement) {
		node.focus();
		node.select();
	}
	// Enter Rename mode for a row: swap its title for an input pre-filled with the
	// current title. stopPropagation so the row's own click never also opens the
	// session; clear any transient action state first.
	function startRename(e: MouseEvent | KeyboardEvent, s: Session) {
		e.stopPropagation();
		actionsOpenId = null;
		disarmDeleteConfirm();
		renamingSessionId = s.id;
		renameValue = s.title || '';
	}
	function cancelRename() {
		renamingSessionId = null;
		renameValue = '';
	}
	// Commit the edited title. Empty/whitespace-only OR unchanged titles cancel
	// with no API call (backend enforces any further length rules). On success the
	// row title updates through the sessions store and, if this session is the one
	// open in the chat tab, its header title updates through the active-session
	// store. Guarded against double-fire: Enter/Escape both null renamingSessionId
	// which unmounts the input and fires a trailing blur — the guard drops it.
	async function commitRename(s: Session) {
		if (renamingSessionId !== s.id) return;
		const next = renameValue.trim();
		const current = (s.title || '').trim();
		renamingSessionId = null;
		renameValue = '';
		if (!next || next === current) return;
		try {
			await appData.renameSession(s.id, next);
			activeSession.setTitle(s.id, next);
		} catch {
			toast.push('Failed to rename session', { tone: 'error' });
		}
	}
	// Keydown on the rename input: Enter commits, Escape cancels. Every key stops
	// propagation so the row's own keydown (Enter/Space → open session) never fires
	// and typing spaces doesn't trigger it either.
	function onRenameKeydown(e: KeyboardEvent, s: Session) {
		e.stopPropagation();
		if (e.key === 'Enter') {
			e.preventDefault();
			commitRename(s);
		} else if (e.key === 'Escape') {
			e.preventDefault();
			cancelRename();
		}
	}

	// Open a session from its row — but never while that row is showing its
	// delete confirmation (deletingSessionId) OR its delete request is in flight
	// (deleteInFlightId), NOR while its title is being renamed inline
	// (renamingSessionId). The confirm button is natively `disabled` during the
	// request, and the browser routes a click on a disabled button to its parent
	// (the row), which would otherwise open the just-deleted session as an empty
	// chat. Two delete variables because the armed-confirm state can be nulled at
	// any moment (Escape / Cancel / re-arm) while the request is still running.
	function openSession(id: string) {
		if (deletingSessionId === id || deleteInFlightId === id) return;
		if (renamingSessionId === id) return;
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
					{#if active.length}
						<section class="board-section">
							<!-- 'pinned' is the legacy persisted collapse key for the Active
							     section (the group was formerly Favorites/Pinned). Kept as-is to
							     preserve users' saved collapse preference across the rename. -->
							<button class="section-label section-toggle" onclick={() => toggleSection('pinned')} aria-expanded={!isCollapsed('pinned')}>
								<ChevronDown size={13} strokeWidth={2.2} class="section-caret {isCollapsed('pinned') ? 'collapsed' : ''}" />
								<CircleDot size={12} strokeWidth={2} class="section-icon" />
								Active <span class="section-count">{active.length}</span>
							</button>
							{#if !isCollapsed('pinned')}
								<div class="row-list">
									{#each active as s (s.id)}{@render sessionRow(s, true)}{/each}
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
	<!-- Armed = the clear confirm is showing OR its request is in flight. While
	     armed, the rail freezes: every non-confirm action is inert and the icons
	     never move, so a second/stray click can only ever hit Confirm or Cancel —
	     never a different action (the old reflow silently fired a neighbour
	     action instead of clearing). -->
	{@const isArmed = deletingSessionId === session.id || deleteInFlightId === session.id}
	<div
		class="row"
		class:active={activeId === session.id}
		class:deleting={isArmed}
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
				{#if renamingSessionId === session.id}
					<input
						class="row-title-input"
						type="text"
						bind:value={renameValue}
						use:focusSelect
						onclick={(e) => e.stopPropagation()}
						onkeydown={(e) => onRenameKeydown(e, session)}
						onblur={() => commitRename(session)}
						aria-label="Rename session"
					/>
				{:else}
					<span class="row-title">{session.title || 'Untitled'}</span>
				{/if}
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
				{#if session.degraded}
					<!-- V2 Q7: a queued turn with no capable runner online. Pure
					     informational — read-only until an owning device returns.
					     Warning tokens; visually distinct from the awaiting-input pill. -->
					<span
						class="row-degraded"
						title="This session has queued work but no device is online to run it — read-only until one returns"
					>
						<MonitorOff size={11} strokeWidth={1.6} />
						<span class="row-degraded-label">Waiting — device offline</span>
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
				{#if session.worktree_branch || session.branch}
					<span class="row-branch"><GitBranch size={11} strokeWidth={1.6} />{session.worktree_branch || session.branch}</span>
				{/if}
			</div>
		</div>
		<span class="row-time">{relTime(session.updated_at || session.created_at)}</span>
		<RowActionMenu
			open={actionsOpenId === session.id}
			onToggle={(e) => toggleRowActions(e, session.id)}
			label="Session actions"
		>
			<!-- Cancel is a LEADING icon that only exists while armed. Because the
			     rail is right-anchored, growing it on the LEFT leaves every
			     following icon (Fork / Complete / delete) at its exact resting x — so
			     the trash icon the user just clicked stays put and a second click
			     lands on Confirm, never a shifted neighbour. -->
			{#if isArmed}
				<button class="row-action cancel" title="Cancel" onclick={cancelDelete}>
					<X size={13} strokeWidth={2} />
				</button>
			{/if}
			{#if session.is_remote}
				<button
					class="row-action"
					title={resumeTitle(session)}
					disabled={isArmed}
					onclick={(e) => handleResume(e, session)}
				>
					<Play size={13} strokeWidth={1.6} />
				</button>
			{/if}
			<button
				class="row-action"
				title="Rename session"
				disabled={isArmed}
				onclick={(e) => startRename(e, session)}
			>
				<Pencil size={13} strokeWidth={1.6} />
			</button>
			<button
				class="row-action"
				title="Fork session"
				disabled={isArmed}
				onclick={(e) => handleFork(e, session)}
			>
				<GitFork size={13} strokeWidth={1.6} />
			</button>
			<!-- State-based Active toggle: an active row gets "Complete" (drops it into
			     Recent), an inactive row gets "Move to Active". State- not group-based
			     so mixed opened-project lists render the right action per row. -->
			<button
				class="row-action"
				title={session.is_active ? 'Complete' : 'Move to Active'}
				aria-label={session.is_active ? 'Complete session' : 'Move session to Active'}
				disabled={isArmed}
				onclick={(e) => handleToggleActive(e, session)}
			>
				{#if session.is_active}
					<Check size={13} strokeWidth={1.6} />
				{:else}
					<Undo2 size={13} strokeWidth={1.6} />
				{/if}
			</button>
			<!-- ONE trash icon that stays mounted through arm → in-flight, keeping its
			     slot (rightmost) fixed: it only swaps resting→danger styling and its
			     handler intent (arm, then confirm). Disabled only while the request
			     is actually in flight so the resting Delete can't be re-armed mid-delete. -->
			<button
				class="row-action broom"
				class:clear={!isArmed}
				class:danger={isArmed}
				disabled={deleteLoading}
				title={isArmed ? 'Confirm delete' : 'Delete session'}
				aria-label={isArmed ? 'Confirm delete' : 'Delete session'}
				onclick={(e) => handleDelete(e, session.id)}
			>
				<Trash2 size={13} strokeWidth={isArmed ? 2 : 1.6} />
			</button>
		</RowActionMenu>
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
	/* Inline rename input — sits in the title's slot and matches its type so the
	   row doesn't reflow on entering edit mode. Primary-ringed to read as active. */
	.row-title-input {
		flex: 1;
		min-width: 0;
		font-family: inherit;
		font-size: 0.85rem;
		font-weight: 500;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--primary);
		border-radius: 6px;
		padding: 2px 6px;
		margin: -3px 0;
		outline: none;
	}
	.row-title-input:focus-visible {
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 30%, transparent);
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

	/* V2 Q7 degraded chip — queued work, no runner online. Warning tokens, but a
	   static outlined pill (no pulse) so it reads as an informational state, not
	   the action-needed awaiting-input pill. */
	.row-degraded {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		flex-shrink: 0;
		max-width: 180px;
		padding: 1px 7px;
		border-radius: 999px;
		border: 1px solid color-mix(in oklch, var(--warning) 40%, transparent);
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 12%, transparent);
	}
	.row-degraded :global(svg) { flex-shrink: 0; }
	.row-degraded-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
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

	/* Row-hover reveal of the action rail is signalled to RowActionMenu through
	   the inherited --ram-reveal custom property (0 hidden / 1 shown); it owns the
	   button opacity + the ⋮ collapse. Keep everything else (size, colour) here. */
	.row { --ram-reveal: 0; }
	.row:hover { --ram-reveal: 1; }
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
	}
	.row-action:hover { color: var(--foreground); background: color-mix(in oklch, var(--muted) 60%, transparent); }
	/* Armed-delete + its cancel stay visible at rest, even unhovered — the armed
	   confirm must never depend on hover to be seen. */
	.row-action.cancel { --ram-reveal: 1; }
	.row-action.danger {
		position: relative;
		overflow: hidden;
		--ram-reveal: 1;
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}
	.row-action.danger:hover { background: color-mix(in oklch, var(--destructive) 20%, transparent); }
	/* Subtle 8s countdown: a hairline under the armed broom drains left-to-right,
	   matching the safety auto-reset in armDeleteConfirm() so the confirm never
	   vanishes "for no reason". Purely decorative — no layout, no pointer surface. */
	.row-action.danger::after {
		content: '';
		position: absolute;
		left: 0;
		bottom: 0;
		height: 2px;
		width: 100%;
		background: var(--destructive);
		transform-origin: left center;
		animation: row-confirm-countdown 8s linear forwards;
	}
	@keyframes row-confirm-countdown {
		from { transform: scaleX(1); }
		to { transform: scaleX(0); }
	}
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

	/* Touch / mobile-width: the rail collapses behind a ⋮ owned by RowActionMenu,
	   which drives its own open/closed button opacity + slide-out — so the desktop
	   --ram-reveal hover signal is moot here and needs no override. */
	@media (max-width: 639px) {
		.board-header { padding: 12px 16px; }
		.board-body { padding: 12px 16px 28px; }
		.proj-grid { grid-template-columns: 1fr; }
		.row { min-height: 64px; padding: 11px 14px; gap: 11px; }
		.row-title { font-size: 0.9rem; }
		.row-title-input { font-size: 0.9rem; }
		.row-line2 { font-size: 0.72rem; gap: 7px; }
		.row-action { width: 38px; height: 38px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.proj-card { transition: none; }
		.section-toggle :global(.section-caret) { transition: none; }
		.dot-streaming, .dot-attention { animation: none; }
		/* No draining countdown — just hold the hairline as a static armed cue. */
		.row-action.danger::after { animation: none; transform: scaleX(1); }
	}
</style>
