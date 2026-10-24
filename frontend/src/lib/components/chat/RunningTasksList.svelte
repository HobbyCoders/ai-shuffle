<script lang="ts">
	/**
	 * RunningTasksList — the chat's running background work in one list (#1816).
	 *
	 * Agents (subagents and swarms), Shell tasks and Watches, grouped. A swarm is ONE row
	 * that expands to its member tasks, each with its own state, transcript and
	 * stop, plus a stop for the whole swarm. Agent rows open the subagent's chat.
	 *
	 * variant="inline" sits under the last reply. On desktop it is the grouped
	 * list (capped height past a handful of rows, with "Show all" for search and
	 * filters). In the mobile shell it is one summary row that opens the sheet.
	 *
	 * variant="sheet" is the full list as a modal: a full-height page on a phone
	 * (swipe a row left to stop it), a centred panel on desktop. A sticky head
	 * carries search, All / Agents / Shell chips and Stop all with a confirm
	 * that names how many it stops. `onClose` is called by Back, Escape and the
	 * scrim; the caller unmounts it.
	 *
	 * Watches (#371 step 3) are the owner's, not this chat's, so every chat
	 * lists all of them: what each watches, when it last fired and its cost,
	 * with Pause / Resume and Stop (terminal, so it confirms). Stop all never
	 * stops a watch.
	 *
	 * Reads the session store's activeTasks, the swarms store and the watches
	 * store; it keeps no
	 * copy of either. Whether the inline list is collapsed is an account
	 * preference (runningTasksOpen), the same in every chat. The only local
	 * state is UI: filter, search, which swarms are open, stops in flight, and
	 * rows the user stopped here (so they read "Stopped" and name the worker
	 * until the list closes).
	 */
	import { onDestroy, tick, untrack } from 'svelte';
	import {
		Activity,
		ArrowLeft,
		Bot,
		ChevronDown,
		CircleCheck,
		Eye,
		LoaderCircle,
		MessageSquareText,
		Pause,
		Play,
		Search,
		Terminal,
		Workflow,
		X
	} from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { runningTasksOpen } from '$lib/stores/expansion-preferences';
	import { swarms } from '$lib/stores/swarms';
	import { watches } from '$lib/stores/watches';
	import { usageTargetDeviceName } from '$lib/stores/usage-target';
	import { stopSessionTask } from '$lib/api/sessions';
	import { cancelLimitResume } from '$lib/api/limit-resume';
	import { pausedLabel, pausedWorkers } from './limit-resume';
	import { notify } from '$lib/stores/notifications';
	import { openSession } from '$lib/services/session-navigation';
	import { buildShell } from '$lib/utils/platform';
	import { isSwarmActive } from '$lib/utils/swarm';
	import SwarmHistory from './SwarmHistory.svelte';
	import {
		buildRows,
		buildWatchRows,
		countRows,
		countSummary,
		formatElapsed,
		formatFired,
		formatWatchCost,
		groupRows,
		memberStateLabel,
		plural,
		stopAllQuestion,
		stoppableRows,
		visibleRows,
		visibleWatches,
		watchCostTitle,
		watchCount,
		watchSummary,
		type RunFilter,
		type RunRow,
		type StoppedRecord,
		type SwarmMemberRow,
		type SwarmRow,
		type WatchListRow
	} from './running-tasks';

	interface Props {
		sessionId: string | null | undefined;
		variant?: 'sheet' | 'inline';
		/** Close the sheet (Back, Escape, scrim). The caller unmounts it. */
		onClose?: () => void;
	}

	let { sessionId, variant = 'inline', onClose }: Props = $props();

	const mobileShell = buildShell() === 'mobile';
	/** Inline rows shown before the list caps its height and offers Show all. */
	const INLINE_CAP = 6;
	const titleId = $props.id();

	// ── Data (store-backed, no copies) ────────────────────────────────────────
	const live = $derived(!!sessionId && $activeSession.sessionId === sessionId);
	const tasks = $derived(live ? ($activeSession.activeTasks ?? []) : []);
	const messages = $derived(live ? $activeSession.messages : []);
	const sessionSwarms = $derived(Object.values($swarms.bySession[sessionId ?? ''] ?? {}));
	const swarmError = $derived($swarms.errors[sessionId ?? ''] || '');
	const defaultWorker = $derived($usageTargetDeviceName || 'This worker');

	const hydrationId = $derived(sessionId ?? null);
	$effect(() => {
		if (hydrationId) void swarms.hydrate(hydrationId);
	});
	// Keep the owner's watches fresh while this list is mounted.
	$effect(() => watches.track());

	// ── UI state ──────────────────────────────────────────────────────────────
	let filter = $state<RunFilter>('all');
	let query = $state('');
	let confirmingAll = $state(false);
	let confirmingSwarm = $state<string | null>(null);
	let expanded = $state<Record<string, boolean>>({});
	let stopping = $state<Set<string>>(new Set());
	let stopped = $state<Record<string, StoppedRecord>>({});
	let confirmingWatch = $state<string | null>(null);
	/** Watches stopped from this list: they read "Stopped" until it closes. */
	let stoppedWatches = $state<Set<string>>(new Set());
	/** Watch rows whose Pause or Resume is in flight. */
	let watchBusy = $state<Set<string>>(new Set());
	// Collapsed or open is the owner's choice for every chat, kept with the
	// account (ui:running_tasks_open): a session switch, a remount or a task
	// starting or ending never reopens a list they collapsed.
	const inlineOpen = $derived($runningTasksOpen);
	const toggleInline = () => runningTasksOpen.set(!runningTasksOpen.current);
	let ownSheetOpen = $state(false);
	let sheetOpener: HTMLElement | null = null;

	// A different chat starts clean.
	let lastSession: string | null | undefined = untrack(() => sessionId);
	$effect(() => {
		const id = sessionId;
		untrack(() => {
			if (id === lastSession) return;
			lastSession = id;
			resetState();
		});
	});

	function resetState() {
		filter = 'all';
		query = '';
		confirmingAll = false;
		confirmingSwarm = null;
		expanded = {};
		stopping = new Set();
		stopped = {};
		confirmingWatch = null;
		stoppedWatches = new Set();
		watchBusy = new Set();
		ownSheetOpen = false;
	}

	const rows = $derived(buildRows({ tasks, swarms: sessionSwarms, messages, stopping, stopped, defaultWorker }));
	// Workers a usage limit paused, by task id: their resume time (#1831).
	const paused = $derived(pausedWorkers(messages));
	const counts = $derived(countRows(rows));
	const hasRunning = $derived(counts.running > 0);
	const shown = $derived(visibleRows(rows, filter, query));
	const groups = $derived(groupRows(shown));
	const stopScope = $derived(stoppableRows(shown));
	const inlineGroups = $derived(groupRows(rows));
	// Finished workflows stay reviewable here once they leave the running list.
	const finishedSwarms = $derived(
		sessionSwarms.filter((w) => !isSwarmActive(w) && !rows.some((r) => r.type === 'swarm' && r.id === w.id)).length
	);
	const showFinished = $derived(finishedSwarms > 0 && filter !== 'shell' && !query.trim());
	const watchRows = $derived(buildWatchRows($watches.list, { stopping, stoppedHere: stoppedWatches }));
	const liveWatches = $derived(watchRows.filter((w) => w.state !== 'stopped').length);
	const pausedWatches = $derived(watchRows.filter((w) => w.state === 'paused').length);
	const shownWatches = $derived(visibleWatches(watchRows, filter, query));
	/** Something to show: running work, or a watch that is not stopped. */
	const hasContent = $derived(hasRunning || liveWatches > 0);
	const allRowCount = $derived(rows.length + watchRows.length);
	const inlineLineCount = $derived(
		rows.reduce((n, r) => n + 1 + (r.type === 'swarm' && expanded[r.key] ? r.members.length : 0), 0) +
			watchRows.length
	);
	/** "2 agents · 1 shell task · 1 watch", or just the watches. */
	const fullSummary = $derived(
		[counts.running ? countSummary(counts) : '', watchSummary(watchRows)].filter(Boolean).join(' · ') || 'Nothing running'
	);
	const headLabel = $derived(counts.running ? plural(counts.running, 'running task') : watchCount(liveWatches));
	const headSub = $derived(
		counts.running
			? fullSummary
			: pausedWatches
				? pausedWatches === liveWatches
					? 'All paused'
					: `${pausedWatches} paused`
				: 'Watching for changes'
	);

	// Rows that stopped and left the running list are remembered; once nothing
	// runs, the inline list goes away and forgets them.
	$effect(() => {
		if (variant === 'inline' && !hasContent && !ownSheetOpen) {
			untrack(() => {
				if (Object.keys(stopped).length) stopped = {};
				if (stoppedWatches.size) stoppedWatches = new Set();
			});
		}
	});

	// A stop in flight whose row has left the running list is settled.
	$effect(() => {
		const runningKeys = new Set(rows.filter((r) => r.state !== 'stopped').map((r) => r.key));
		untrack(() => {
			let changed = false;
			const next = new Set(stopping);
			for (const key of stopping) {
				if (!runningKeys.has(key) && stopped[key]) {
					next.delete(key);
					changed = true;
				}
			}
			if (changed) stopping = next;
		});
	});

	// Confirm only while it still has something to stop.
	$effect(() => {
		if (confirmingAll && stopScope.length === 0) untrack(() => (confirmingAll = false));
	});

	// ── Live clock ────────────────────────────────────────────────────────────
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;
	const sheetVisible = $derived(variant === 'sheet' || ownSheetOpen);
	const onScreen = $derived(sheetVisible || (variant === 'inline' && inlineOpen && !mobileShell));
	// Elapsed times tick each second; a watch's "Fired 5m ago" needs far less.
	const tickMs = $derived(!onScreen ? 0 : hasRunning ? 1000 : watchRows.length ? 30_000 : 0);
	let timerMs = 0;
	$effect(() => {
		const ms = tickMs;
		untrack(() => {
			if (ms === timerMs) return;
			if (timer !== null) clearInterval(timer);
			timer = null;
			timerMs = ms;
			if (ms) {
				now = Date.now();
				timer = setInterval(() => (now = Date.now()), ms);
			}
		});
	});
	onDestroy(() => {
		if (timer !== null) clearInterval(timer);
	});

	function elapsedOf(row: { startedAt: number | null; endedAt: number | null }): string {
		return formatElapsed(row.startedAt, row.endedAt ?? now);
	}

	// ── Actions ───────────────────────────────────────────────────────────────
	function markStopping(key: string, on: boolean) {
		const next = new Set(stopping);
		if (on) next.add(key);
		else next.delete(key);
		stopping = next;
	}

	function remember(row: RunRow) {
		stopped = { ...stopped, [row.key]: { row: { ...row, endedAt: Date.now() }, at: Date.now() } };
	}

	async function stopTask(id: string): Promise<boolean> {
		if (!sessionId) return false;
		try {
			// A paused worker is stopped by cancelling its scheduled resume.
			if (paused.has(id)) return (await cancelLimitResume(sessionId, id)).cancelled;
			const result = await stopSessionTask(sessionId, id);
			return result.ok;
		} catch {
			return false;
		}
	}

	/** Stop one row. `quiet` leaves the error toast to the caller (Stop all). */
	async function stopRow(row: RunRow, quiet = false): Promise<boolean> {
		if (!sessionId || row.state !== 'running') return false;
		markStopping(row.key, true);
		let ok = false;
		if (row.type === 'swarm') {
			try {
				await swarms.cancel(sessionId, row.id);
				ok = true;
			} catch (error) {
				if (!quiet) notify.error(error instanceof Error ? error.message : "Couldn't stop the swarm. Try again.");
			}
		} else {
			ok = await stopTask(row.id);
			if (!ok && !quiet) notify.error("Couldn't stop the task. It may have already finished.");
		}
		if (ok) remember(row);
		else markStopping(row.key, false);
		return ok;
	}

	async function stopMember(swarm: SwarmRow, member: SwarmMemberRow) {
		if (!member.taskId || member.status !== 'running' || stopping.has(member.key)) return;
		markStopping(member.key, true);
		const ok = await stopTask(member.taskId);
		if (!ok) {
			markStopping(member.key, false);
			notify.error("Couldn't stop the task. It may have already finished.");
		}
		// On success the swarm's next snapshot marks it cancelled; the spinner
		// stays until then.
	}

	async function stopAll() {
		const scope = stopScope;
		confirmingAll = false;
		if (!scope.length) return;
		const results = await Promise.all(scope.map((row) => stopRow(row, true)));
		const failed = results.filter((ok) => !ok).length;
		if (failed) notify.error(`Couldn't stop ${plural(failed, 'task')}. ${failed === 1 ? 'It' : 'They'} may have already finished.`);
	}

	async function confirmSwarmStop(row: SwarmRow) {
		confirmingSwarm = null;
		await stopRow(row);
	}

	function requestStop(row: RunRow) {
		if (row.state !== 'running') return;
		if (row.type === 'swarm') confirmingSwarm = row.key;
		else void stopRow(row);
	}

	// ── Watches ───────────────────────────────────────────────────────────────
	function markWatchBusy(key: string, on: boolean) {
		const next = new Set(watchBusy);
		if (on) next.add(key);
		else next.delete(key);
		watchBusy = next;
	}

	async function toggleWatch(row: WatchListRow) {
		if (watchBusy.has(row.key) || !(row.canPause || row.canResume)) return;
		const pausing = row.state === 'active';
		markWatchBusy(row.key, true);
		try {
			await watches.setStatus(row.id, pausing ? 'paused' : 'active');
		} catch {
			notify.error(pausing ? "Couldn't pause the watch. Try again." : "Couldn't resume the watch. Try again.");
		} finally {
			markWatchBusy(row.key, false);
		}
	}

	async function stopWatch(row: WatchListRow) {
		confirmingWatch = null;
		if (!row.canStop || stopping.has(row.key)) return;
		markStopping(row.key, true);
		// Kept before the stop lands, so the row reads "Stopped" instead of vanishing.
		stoppedWatches = new Set([...stoppedWatches, row.id]);
		try {
			await watches.setStatus(row.id, 'stopped');
		} catch {
			const next = new Set(stoppedWatches);
			next.delete(row.id);
			stoppedWatches = next;
			notify.error("Couldn't stop the watch. Try again.");
		} finally {
			markStopping(row.key, false);
		}
	}

	function openTranscript(id: string | null) {
		if (!id) return;
		closeSheet();
		openSession(id);
	}

	function toggleSwarm(key: string) {
		expanded = { ...expanded, [key]: !expanded[key] };
	}

	// ── Sheet open / close / focus ────────────────────────────────────────────
	let sheetRef = $state<HTMLElement | null>(null);
	let closeRef = $state<HTMLButtonElement | null>(null);

	function openOwnSheet(event: MouseEvent) {
		sheetOpener = event.currentTarget as HTMLElement;
		ownSheetOpen = true;
	}

	function closeSheet() {
		confirmingAll = false;
		confirmingSwarm = null;
		confirmingWatch = null;
		if (variant === 'sheet') {
			onClose?.();
			return;
		}
		if (!ownSheetOpen) return;
		ownSheetOpen = false;
		query = '';
		filter = 'all';
		const opener = sheetOpener;
		sheetOpener = null;
		void tick().then(() => opener?.focus({ preventScroll: true }));
	}

	$effect(() => {
		if (sheetVisible && closeRef) {
			const el = closeRef;
			void tick().then(() => el.focus({ preventScroll: true }));
		}
	});

	function onSheetKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.preventDefault();
			event.stopPropagation();
			if (confirmingAll) confirmingAll = false;
			else if (confirmingSwarm) confirmingSwarm = null;
			else if (confirmingWatch) confirmingWatch = null;
			else closeSheet();
			return;
		}
		if (event.key !== 'Tab' || !sheetRef) return;
		// Keep focus inside the modal.
		const focusable = Array.from(
			sheetRef.querySelectorAll<HTMLElement>('button:not(:disabled), input, [tabindex]:not([tabindex="-1"])')
		).filter((el) => el.offsetParent !== null || el === document.activeElement);
		if (!focusable.length) return;
		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		if (event.shiftKey && document.activeElement === first) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && document.activeElement === last) {
			event.preventDefault();
			first.focus();
		}
	}

	/** Render the modal at the document root so no transformed ancestor clips it. */
	function portal(node: HTMLElement) {
		if (typeof document === 'undefined') return;
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
	}

	// ── Swipe left to stop (touch and pen only) ───────────────────────────────
	const SWIPE_ARM = 0.35;
	let swipe = $state<{ key: string; dx: number; armed: boolean } | null>(null);
	let swipeStart: { key: string; x: number; y: number; id: number; width: number; locked: boolean | null } | null = null;
	let suppressClickUntil = 0;

	function swipeDown(event: PointerEvent, row: RunRow) {
		if (event.pointerType === 'mouse' || row.state !== 'running' || !event.isPrimary) return;
		const target = event.currentTarget as HTMLElement;
		swipeStart = { key: row.key, x: event.clientX, y: event.clientY, id: event.pointerId, width: target.offsetWidth || 360, locked: null };
	}

	function swipeMove(event: PointerEvent) {
		const s = swipeStart;
		if (!s || event.pointerId !== s.id) return;
		const dx = event.clientX - s.x;
		const dy = event.clientY - s.y;
		if (s.locked === null) {
			if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
			// Vertical intent scrolls the list; only a sideways drag swipes.
			s.locked = Math.abs(dx) > Math.abs(dy) && dx < 0;
			if (!s.locked) {
				swipeStart = null;
				return;
			}
			(event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
		}
		const clamped = Math.min(0, dx);
		swipe = { key: s.key, dx: clamped, armed: -clamped >= s.width * SWIPE_ARM };
	}

	function swipeEnd(event: PointerEvent, row: RunRow) {
		const s = swipeStart;
		if (!s || event.pointerId !== s.id) return;
		swipeStart = null;
		const armed = swipe?.key === row.key && swipe.armed;
		const moved = swipe?.key === row.key && swipe.dx < -8;
		swipe = null;
		if (moved) suppressClickUntil = Date.now() + 350;
		if (armed) requestStop(row);
	}

	function swipeCancel() {
		swipeStart = null;
		swipe = null;
	}

	function guardClick(event: MouseEvent, run: () => void) {
		if (Date.now() < suppressClickUntil) {
			event.preventDefault();
			return;
		}
		run();
	}

	// The Watches chip shows once there is a watch (or while it is chosen).
	const filters = $derived<{ id: RunFilter; label: string }[]>([
		{ id: 'all', label: 'All' },
		{ id: 'agents', label: 'Agents' },
		{ id: 'shell', label: 'Shell' },
		...(watchRows.length || filter === 'watches' ? [{ id: 'watches' as const, label: 'Watches' }] : [])
	]);
	const filterCount = (id: RunFilter) =>
		id === 'all' ? counts.running + liveWatches : id === 'agents' ? counts.agents : id === 'shell' ? counts.shell : liveWatches;
	const emptyMessage = $derived(
		query.trim()
			? `No task matches “${query.trim()}”`
			: filter === 'shell'
				? 'No shell tasks running'
				: filter === 'agents'
					? 'No agents running'
					: filter === 'watches'
						? 'No watches. Ask any chat to watch something for you.'
						: 'Nothing running'
	);
</script>

<!-- ── One row (task or swarm), shared by both variants ─────────────────── -->
{#snippet rowView(row: RunRow, size: 'compact' | 'roomy')}
	{@const swiping = swipe?.key === row.key ? swipe : null}
	{@const isSwarm = row.type === 'swarm'}
	{@const open = isSwarm && !!expanded[row.key]}
	<li
		class="row size-{size}"
		class:is-stopped={row.state === 'stopped'}
		class:is-stopping={row.state === 'stopping'}
		class:armed={swiping?.armed}
		data-row={row.key}
		data-state={row.state}
	>
		{#if size === 'roomy'}
			<div class="row-under" aria-hidden="true"><X size={16} strokeWidth={2.5} />Stop</div>
		{/if}
		<div
			class="row-inner"
			class:dragging={!!swiping}
			style:transform={swiping ? `translateX(${swiping.dx}px)` : undefined}
			onpointerdown={size === 'roomy' ? (e) => swipeDown(e, row) : undefined}
			onpointermove={size === 'roomy' ? swipeMove : undefined}
			onpointerup={size === 'roomy' ? (e) => swipeEnd(e, row) : undefined}
			onpointercancel={size === 'roomy' ? swipeCancel : undefined}
			role="presentation"
		>
			<span class="row-icon kind-{row.kind}" class:swarm={isSwarm} aria-hidden="true">
				{#if isSwarm}<Workflow size={size === 'roomy' ? 16 : 13} strokeWidth={1.8} />
				{:else if row.kind === 'shell'}<Terminal size={size === 'roomy' ? 16 : 13} strokeWidth={1.8} />
				{:else}<Bot size={size === 'roomy' ? 16 : 13} strokeWidth={1.8} />{/if}
			</span>
			{#if isSwarm}
				<button
					type="button"
					class="row-name as-button"
					aria-expanded={open}
					aria-label="Swarm {row.name}, {plural(row.members.length, 'task')}. {open ? 'Hide' : 'Show'} its tasks"
					onclick={(e) => guardClick(e, () => toggleSwarm(row.key))}
				>
					<span class="name-text" title={row.name}>{row.name}</span>
					<span class="swarm-count">{row.runningMembers}/{row.members.length}</span>
					<ChevronDown size={14} class={open ? 'swarm-chev open' : 'swarm-chev'} aria-hidden="true" />
				</button>
			{:else}
				<span class="row-name" class:mono={row.kind === 'shell'} title={row.name}><span class="name-text">{row.name}</span>{#if row.model}<span class="model-chip" title="Model: {row.model.raw}">{row.model.label}</span>{/if}</span>
			{/if}
			<span class="row-meta">
				<b class="meta-time">{row.state === 'stopped' ? 'Stopped' : elapsedOf(row)}</b>
				<span class="meta-worker" title="Runs on {row.worker}">{row.worker}</span>
			</span>
			<span class="row-actions">
				{#if row.type === 'task' && row.kind === 'agent' && row.transcriptSessionId}
					<button
						type="button"
						class="icon-btn"
						title="Open transcript"
						aria-label="Open transcript: {row.name}"
						onclick={(e) => guardClick(e, () => openTranscript(row.transcriptSessionId))}
					><MessageSquareText size={size === 'roomy' ? 18 : 14} strokeWidth={1.7} /></button>
				{:else}
					<span class="icon-slot" aria-hidden="true"></span>
				{/if}
				{#if row.state === 'running'}
					{@const stopLabel = isSwarm ? 'Stop swarm' : paused.has(row.id) ? 'Cancel' : 'Stop'}
					<button
						type="button"
						class="icon-btn stop"
						title={stopLabel}
						aria-label="{stopLabel}: {row.name}"
						onclick={(e) => guardClick(e, () => requestStop(row))}
					><X size={size === 'roomy' ? 18 : 13} strokeWidth={2.5} /></button>
				{:else if row.state === 'stopping'}
					<span class="icon-btn stop busy" role="status" aria-label="Stopping {row.name}"><LoaderCircle size={size === 'roomy' ? 17 : 13} class="spin" /></span>
				{:else}
					<span class="icon-slot" aria-hidden="true"></span>
				{/if}
			</span>
		</div>
		{#if !isSwarm && row.state === 'running' && paused.has(row.id)}
			<div class="paused-note in-row" role="status">{pausedLabel(paused.get(row.id) ?? 0)}</div>
		{/if}
		{#if isSwarm && confirmingSwarm === row.key && row.state === 'running'}
			<div class="confirm in-row" role="alertdialog" aria-label="Stop swarm {row.name}">
				<span>Stop this swarm and its {plural(row.members.filter((m) => m.status === 'running' || m.status === 'queued' || m.status === 'blocked').length, 'unfinished task')}?</span>
				<button type="button" class="btn ghost" onclick={() => (confirmingSwarm = null)}>Keep</button>
				<button type="button" class="btn danger" onclick={() => confirmSwarmStop(row)}>Stop swarm</button>
			</div>
		{/if}
		{#if row.type === 'swarm' && open}
			<ul class="members" aria-label="Tasks in {row.name}">
				{#each row.members as member (member.key)}
					{@const memberStopping = stopping.has(member.key) && member.status === 'running'}
					<li class="member size-{size}" data-status={member.status}>
						<span class="member-dot" aria-hidden="true"></span>
						<span class="row-name" title={member.agent ? `${member.title} · ${member.agent}` : member.title}>
							<span class="name-text">{member.title}</span>
							{#if member.agent}<span class="member-agent">{member.agent}</span>{/if}
							{#if member.model}<span class="model-chip" title="Model: {member.model.raw}">{member.model.label}</span>{/if}
						</span>
						<span class="row-meta">
							<b class="meta-time">{member.status === 'running' ? formatElapsed(member.startedAt, member.endedAt ?? now) : memberStateLabel(member.status)}</b>
							<span class="meta-worker">{row.worker}</span>
						</span>
						<span class="row-actions">
							{#if member.transcriptSessionId}
								<button type="button" class="icon-btn" title="Open transcript" aria-label="Open transcript: {member.title}"
									onclick={() => openTranscript(member.transcriptSessionId)}>
									<MessageSquareText size={size === 'roomy' ? 18 : 14} strokeWidth={1.7} />
								</button>
							{:else}
								<span class="icon-slot" aria-hidden="true"></span>
							{/if}
							{#if memberStopping}
								<span class="icon-btn stop busy" role="status" aria-label="Stopping {member.title}"><LoaderCircle size={size === 'roomy' ? 17 : 13} class="spin" /></span>
							{:else if member.status === 'running' && member.taskId}
								<button type="button" class="icon-btn stop" title="Stop" aria-label="Stop: {member.title}"
									onclick={() => stopMember(row, member)}>
									<X size={size === 'roomy' ? 18 : 13} strokeWidth={2.5} />
								</button>
							{:else}
								<span class="icon-slot" aria-hidden="true"></span>
							{/if}
						</span>
					</li>
				{/each}
				{#if row.members.length === 0}
					<li class="member-empty">No tasks scheduled yet.</li>
				{/if}
			</ul>
		{/if}
	</li>
{/snippet}

<!-- ── One watch: what it watches, last fired, cost; Pause / Resume, Stop ── -->
{#snippet watchView(row: WatchListRow, size: 'compact' | 'roomy')}
	{@const iconSize = size === 'roomy' ? 18 : 13}
	{@const busy = watchBusy.has(row.key)}
	{@const fired = row.state === 'stopped' ? 'Stopped' : formatFired(row.lastFiredAt, now)}
	<li
		class="row watch size-{size}"
		class:is-stopped={row.state === 'stopped'}
		class:is-stopping={row.state === 'stopping'}
		class:is-paused={row.state === 'paused'}
		data-row={row.key}
		data-state={row.state}
	>
		<div class="row-inner">
			<span class="row-icon kind-watch" aria-hidden="true"><Eye size={size === 'roomy' ? 16 : 13} strokeWidth={1.8} /></span>
			<span class="watch-text">
				<span class="row-name" title={row.name}>
					<span class="name-text">{row.name}</span>
					{#if row.state === 'paused'}<span class="state-chip on-name" class:roomy={size === 'roomy'}>Paused</span>{/if}
				</span>
				<span class="row-meta">
					<!-- In the one-line sheet row the chip leads the meta line instead,
					     so the name keeps its room (CSS picks one of the two). -->
					{#if row.state === 'paused' && size === 'roomy'}<span class="state-chip on-meta">Paused</span>{/if}
					<b
						class="meta-time"
						title={row.lastFiredAt ? `Last fired ${new Date(row.lastFiredAt).toLocaleString()}` : undefined}
					>{#if fired.startsWith('Fired ')}<span class="fired-word">{'Fired '}</span>{fired.slice(6)}{:else}{fired}{/if}</b>
					<span class="meta-worker meta-cost" title={watchCostTitle(row)}>{formatWatchCost(row.costUsd)}</span>
				</span>
			</span>
			<span class="row-actions">
				{#if row.sessionId && row.sessionId !== sessionId && row.state !== 'stopped'}
					<button
						type="button"
						class="icon-btn"
						title="Open the chat it reports to"
						aria-label="Open the chat for watch: {row.name}"
						onclick={() => openTranscript(row.sessionId)}
					><MessageSquareText size={size === 'roomy' ? 18 : 14} strokeWidth={1.7} /></button>
				{:else}
					<span class="icon-slot" aria-hidden="true"></span>
				{/if}
				{#if busy}
					<span class="icon-btn busy" role="status" aria-label="Updating {row.name}"><LoaderCircle size={size === 'roomy' ? 17 : 13} class="spin" /></span>
				{:else if row.canPause}
					<button type="button" class="icon-btn" title="Pause" aria-label="Pause watch: {row.name}" onclick={() => toggleWatch(row)}>
						<Pause size={iconSize} strokeWidth={2} />
					</button>
				{:else if row.canResume}
					<button type="button" class="icon-btn resume" title="Resume" aria-label="Resume watch: {row.name}" onclick={() => toggleWatch(row)}>
						<Play size={iconSize} strokeWidth={2} />
					</button>
				{:else}
					<span class="icon-slot" aria-hidden="true"></span>
				{/if}
				{#if row.state === 'stopping'}
					<span class="icon-btn stop busy" role="status" aria-label="Stopping {row.name}"><LoaderCircle size={size === 'roomy' ? 17 : 13} class="spin" /></span>
				{:else if row.canStop}
					<button
						type="button"
						class="icon-btn stop"
						title="Stop"
						aria-label="Stop watch: {row.name}"
						aria-expanded={confirmingWatch === row.key}
						onclick={() => (confirmingWatch = confirmingWatch === row.key ? null : row.key)}
					><X size={size === 'roomy' ? 18 : 13} strokeWidth={2.5} /></button>
				{:else}
					<span class="icon-slot" aria-hidden="true"></span>
				{/if}
			</span>
		</div>
		{#if row.reason}
			<div class="paused-note in-row" role="status">Paused: {row.reason}</div>
		{/if}
		{#if confirmingWatch === row.key && row.canStop}
			<div class="confirm in-row" role="alertdialog" aria-label="Stop watch {row.name}">
				<span>Stop this watch for good? To watch again, ask for a new one.</span>
				<button type="button" class="btn ghost" onclick={() => (confirmingWatch = null)}>Keep</button>
				<button type="button" class="btn danger" onclick={() => stopWatch(row)}>Stop watch</button>
			</div>
		{/if}
	</li>
{/snippet}

{#snippet watchGroup(list: WatchListRow[], size: 'compact' | 'roomy')}
	{#if list.length}
		<section class="group" aria-labelledby="{titleId}-watches-{size}">
			<h3 class="group-head" id="{titleId}-watches-{size}">
				<span>Watches</span> <span class="group-count">{list.filter((r) => r.state !== 'stopped').length}</span>
			</h3>
			<ul class="rows" role="list">
				{#each list as row (row.key)}
					{@render watchView(row, size)}
				{/each}
			</ul>
		</section>
	{/if}
{/snippet}

{#snippet groupView(label: string, list: RunRow[], size: 'compact' | 'roomy', groupId: string)}
	{#if list.length}
		<section class="group" aria-labelledby="{titleId}-{groupId}-{size}">
			<h3 class="group-head" id="{titleId}-{groupId}-{size}">
				<span>{label}</span> <span class="group-count">{list.filter((r) => r.state !== 'stopped').length}</span>
			</h3>
			<ul class="rows" role="list">
				{#each list as row (row.key)}
					{@render rowView(row, size)}
				{/each}
			</ul>
		</section>
	{/if}
{/snippet}

<!-- ── The full list as a modal ──────────────────────────────────────────── -->
{#snippet sheet()}
	<div class="rt-layer" class:mobile={mobileShell} use:portal>
		<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
		<div class="rt-scrim" aria-hidden="true" onclick={closeSheet}></div>
		<div
			bind:this={sheetRef}
			class="rt-sheet"
			role="dialog"
			aria-modal="true"
			aria-labelledby="{titleId}-title"
			tabindex="-1"
			onkeydown={onSheetKeydown}
		>
			<header class="rt-head">
				<div class="rt-top">
					<button bind:this={closeRef} type="button" class="icon-btn back" aria-label="Close running tasks" onclick={closeSheet}>
						<ArrowLeft size={20} class="back-arrow" /><X size={18} class="back-x" />
					</button>
					<div class="rt-title">
						<h2 id="{titleId}-title">Running tasks</h2>
						<p aria-live="polite">{counts.running ? `${counts.running} running · ${fullSummary}` : fullSummary}</p>
					</div>
					<button
						type="button"
						class="stop-all"
						disabled={stopScope.length === 0 || confirmingAll}
						aria-label="Stop all{stopScope.length ? `: ${plural(stopScope.length, 'task')}` : ''}"
						onclick={() => (confirmingAll = true)}
					>Stop all</button>
				</div>
				{#if confirmingAll}
					<div class="confirm" role="alertdialog" aria-labelledby="{titleId}-confirm">
						<span id="{titleId}-confirm">{stopAllQuestion(stopScope.length, filter)}</span>
						<button type="button" class="btn ghost" onclick={() => (confirmingAll = false)}>Cancel</button>
						<button type="button" class="btn danger" onclick={stopAll}>Stop {stopScope.length}</button>
					</div>
				{/if}
				<label class="rt-search">
					<Search size={16} aria-hidden="true" />
					<input type="search" placeholder="Search tasks" aria-label="Search tasks" bind:value={query} autocomplete="off" />
					{#if query}
						<button type="button" class="clear" aria-label="Clear search" onclick={() => (query = '')}><X size={14} /></button>
					{/if}
				</label>
				<div class="rt-chips" role="group" aria-label="Show" class:four={filters.length > 3}>
					{#each filters as f (f.id)}
						<button
							type="button"
							class="chip"
							aria-pressed={filter === f.id}
							aria-label="{f.label}, {filterCount(f.id)}"
							onclick={() => (filter = f.id)}
						><span class="chip-label">{f.label}</span><span class="chip-count">{filterCount(f.id)}</span></button>
					{/each}
				</div>
			</header>
			<div class="rt-list">
				{#if swarmError && sessionSwarms.length}
					<p class="rt-warn" role="status">Swarm status could not be refreshed. <button type="button" class="link" onclick={() => sessionId && swarms.hydrate(sessionId)}>Retry</button></p>
				{/if}
				{#if $watches.error && watchRows.length}
					<p class="rt-warn" role="status">Watches could not be refreshed. <button type="button" class="link" onclick={() => watches.refresh()}>Retry</button></p>
				{/if}
				{#if shown.length === 0 && shownWatches.length === 0}
					<div class="rt-empty">
						{#if allRowCount === 0}<CircleCheck size={28} aria-hidden="true" />{/if}
						<p>{emptyMessage}</p>
					</div>
				{:else}
					{@render groupView('Agents', groups.agents, 'roomy', 'agents')}
					{@render groupView('Shell', groups.shell, 'roomy', 'shell')}
					{@render watchGroup(shownWatches, 'roomy')}
				{/if}
				{#if showFinished && sessionId}
					<details class="finished">
						<summary>
							<ChevronDown size={14} class="finished-chev" aria-hidden="true" />
							<span>Finished swarms</span> <span class="group-count">{finishedSwarms}</span>
						</summary>
						<div class="finished-body"><SwarmHistory {sessionId} finishedOnly /></div>
					</details>
				{/if}
			</div>
		</div>
	</div>
{/snippet}

{#if variant === 'sheet'}
	{@render sheet()}
{:else if hasContent}
	{#if mobileShell}
		<!-- Phone: one summary row; the full list opens as a sheet. -->
		<button type="button" class="summary-row" onclick={openOwnSheet} aria-haspopup="dialog"
			aria-label="{headLabel}: {headSub}. Open the list">
			<span class="pulse" class:quiet={!hasRunning} aria-hidden="true">{#if hasRunning}<Activity size={16} />{:else}<Eye size={16} />{/if}</span>
			<span class="summary-text"><b>{counts.running ? `${counts.running} running` : headLabel}</b><small>{headSub}</small></span>
			<span class="summary-open">Open</span>
		</button>
	{:else}
		<div class="inline" data-open={inlineOpen}>
			<div class="inline-head">
				<button type="button" class="inline-toggle" onclick={toggleInline} aria-expanded={inlineOpen}
					data-testid="tasks-toggle" title={inlineOpen ? 'Collapse task list' : 'Show running tasks'}>
					{#if hasRunning}
						<span class="spinner" aria-hidden="true"></span>
					{:else}
						<span class="watch-mark" aria-hidden="true"><Eye size={12} strokeWidth={2} /></span>
					{/if}
					<span class="inline-label">{headLabel}</span>
					<span class="inline-sub">{headSub}</span>
					<ChevronDown size={12} class={inlineOpen ? 'inline-chev open' : 'inline-chev'} aria-hidden="true" />
				</button>
				{#if inlineOpen}
					<span class="inline-tools">
						{#if allRowCount > INLINE_CAP}
							<button type="button" class="text-btn" onclick={openOwnSheet} aria-haspopup="dialog">Show all</button>
						{/if}
						{#if counts.running > 1}
							<button type="button" class="text-btn danger" onclick={() => (confirmingAll = true)} disabled={confirmingAll}>Stop all</button>
						{/if}
					</span>
				{/if}
			</div>
			{#if inlineOpen}
				{#if confirmingAll && !ownSheetOpen}
					<div class="confirm compact" role="alertdialog" aria-labelledby="{titleId}-inline-confirm">
						<span id="{titleId}-inline-confirm">{stopAllQuestion(stoppableRows(rows).length, 'all')}</span>
						<button type="button" class="btn ghost" onclick={() => (confirmingAll = false)}>Cancel</button>
						<button type="button" class="btn danger" onclick={() => { filter = 'all'; query = ''; void stopAll(); }}>Stop {stoppableRows(rows).length}</button>
					</div>
				{/if}
				<div class="inline-body" class:capped={inlineLineCount > INLINE_CAP}>
					{@render groupView('Agents', inlineGroups.agents, 'compact', 'agents')}
					{@render groupView('Shell', inlineGroups.shell, 'compact', 'shell')}
					{@render watchGroup(watchRows, 'compact')}
				</div>
			{/if}
		</div>
	{/if}
{/if}

{#if variant === 'inline' && ownSheetOpen}
	{@render sheet()}
{/if}

<style>
	/* ── Shared row ─────────────────────────────────────────────────────────── */
	.rows, .members { list-style: none; margin: 0; padding: 0; }
	.row { position: relative; min-width: 0; }
	.row-inner, .member {
		position: relative;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto auto;
		align-items: center;
		min-width: 0;
		background: var(--background);
	}
	.row-icon {
		display: grid;
		place-items: center;
		flex: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.6);
		background: var(--muted);
		/* Every row type takes the theme's primary; the glyph (bot, terminal,
		   workflow) tells them apart, not a fixed tool colour. */
		color: var(--primary);
	}
	.row-name {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		color: var(--foreground);
		font-weight: 500;
	}
	.row-name.mono .name-text { font-family: var(--font-mono); }
	.name-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.as-button {
		border: 0;
		background: transparent;
		padding: 0;
		font: inherit;
		text-align: left;
		cursor: pointer;
		border-radius: 6px;
	}
	@media (hover: hover) {
		.as-button:hover .name-text { text-decoration: underline; text-underline-offset: 2px; }
	}
	.swarm-count {
		flex: none;
		font-size: 0.6875rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 14%, transparent);
		padding: 0 6px;
		border-radius: 999px;
		line-height: 1.6;
	}
	.row :global(.swarm-chev) { flex: none; color: var(--muted-foreground); transition: transform 0.16s ease; }
	.row :global(.swarm-chev.open) { transform: rotate(180deg); }
	.row-meta {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		min-width: 0;
		max-width: 9rem;
		font-variant-numeric: tabular-nums;
		color: var(--muted-foreground);
		line-height: 1.25;
		text-align: right;
	}
	.meta-time { font-weight: 550; color: var(--foreground); white-space: nowrap; }
	.paused-note {
		margin: 0 0 4px 28px;
		font-size: 0.75rem;
		line-height: 1.3;
		color: var(--muted-foreground);
	}
	.size-roomy .paused-note { margin-left: 44px; font-size: 0.8125rem; }
	.meta-worker { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.is-stopped .meta-time { color: var(--muted-foreground); font-weight: 500; }
	.is-stopped .row-name, .is-stopped .row-icon { opacity: 0.6; }
	.is-stopping .row-name { opacity: 0.7; }
	.row-actions { display: flex; align-items: center; }
	.icon-btn {
		display: grid;
		place-items: center;
		border: 0;
		background: transparent;
		color: var(--muted-foreground);
		border-radius: calc(var(--radius, 0.75rem) * 0.6);
		cursor: pointer;
		padding: 0;
		transition: background-color 0.12s ease, color 0.12s ease;
	}
	.icon-btn:hover { background: var(--hover-overlay); color: var(--foreground); }
	/* Stop is a plain x: quiet at rest, destructive on hover (as before #1826). */
	.icon-btn.stop:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive); }
	.icon-btn.busy { cursor: default; color: var(--muted-foreground); }
	.icon-btn.busy:hover { background: transparent; }
	.icon-btn:focus-visible, .as-button:focus-visible, .chip:focus-visible, .stop-all:focus-visible,
	.btn:focus-visible, .text-btn:focus-visible, .inline-toggle:focus-visible, .summary-row:focus-visible,
	.clear:focus-visible, .link:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.icon-slot { display: block; }
	.icon-btn :global(.spin) { animation: rt-spin 0.9s linear infinite; }
	@keyframes rt-spin { to { transform: rotate(360deg); } }

	/* Members of a swarm */
	.members { border-left: 1px solid var(--border); }
	.member-dot { width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid var(--muted-foreground); justify-self: center; }
	.member[data-status='running'] .member-dot { border-color: var(--primary); border-top-color: transparent; animation: rt-spin 0.9s linear infinite; }
	.member[data-status='completed'] .member-dot { border-color: var(--success); background: var(--success); }
	.member[data-status='failed'] .member-dot { border-color: var(--destructive); background: var(--destructive); }
	.member[data-status='cancelled'] .member-dot, .member[data-status='interrupted'] .member-dot { border-color: var(--muted-foreground); background: var(--muted-foreground); opacity: 0.6; }
	.member[data-status='failed'] .meta-time { color: var(--destructive); }
	.member:not([data-status='running']) .meta-time { font-weight: 500; color: var(--muted-foreground); }
	/* The model a subagent runs on (#1827). Inline on the name line, so the
	   row keeps its height when the label arrives; the name gives way. */
	.model-chip {
		flex: none;
		max-width: 45%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.6875rem;
		font-weight: 500;
		line-height: 1.5;
		padding: 0 6px;
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}
	.size-compact .model-chip { font-size: 0.625rem; padding: 0 5px; }
	.member-agent { flex: none; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 400; font-size: 0.92em; color: var(--muted-foreground); }
	.member-empty { color: var(--muted-foreground); }

	/* Confirms */
	.confirm {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		align-items: center;
		gap: 8px;
		border-radius: var(--radius, 0.75rem);
		background: color-mix(in oklch, var(--destructive) 10%, var(--background));
		border: 1px solid color-mix(in oklch, var(--destructive) 40%, var(--border));
		font-weight: 600;
		color: var(--foreground);
	}
	.btn {
		border-radius: calc(var(--radius, 0.75rem) * 0.7);
		font: inherit;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--foreground);
	}
	.btn.ghost:hover { background: var(--hover-overlay); }
	.btn.danger { background: var(--destructive); border-color: var(--destructive); color: var(--destructive-foreground); }
	.btn.danger:hover { filter: brightness(1.08); }

	/* ── Compact (desktop inline) ───────────────────────────────────────────── */
	.inline { flex-shrink: 0; font-size: 0.75rem; color: var(--muted-foreground); }
	.inline-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
	.inline-toggle {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		padding: 2px 4px;
		margin-left: -4px;
		border: 0;
		background: transparent;
		font: inherit;
		color: inherit;
		cursor: pointer;
		border-radius: 6px;
		transition: color 0.12s ease, background 0.12s ease;
	}
	.inline-toggle:hover { color: var(--foreground); background: var(--hover-overlay); }
	.inline-label { font-weight: 500; white-space: nowrap; color: var(--foreground); }
	.inline-sub { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
	.inline :global(.inline-chev) { flex: none; opacity: 0.7; transform: rotate(-90deg); transition: transform 0.15s ease; }
	.inline :global(.inline-chev.open) { transform: none; }
	.spinner {
		flex: none;
		width: 10px;
		height: 10px;
		border-radius: 50%;
		border: 1.5px solid color-mix(in srgb, var(--primary) 30%, transparent);
		border-top-color: var(--primary);
		animation: rt-spin 0.8s linear infinite;
	}
	.inline-tools { display: flex; align-items: center; gap: 2px; margin-left: auto; flex: none; }
	.text-btn {
		border: 0;
		background: transparent;
		font: inherit;
		font-weight: 500;
		color: var(--muted-foreground);
		padding: 2px 6px;
		border-radius: 6px;
		cursor: pointer;
	}
	.text-btn:hover:not(:disabled) { color: var(--foreground); background: var(--hover-overlay); }
	.text-btn.danger:hover:not(:disabled) { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 10%, transparent); }
	.text-btn:disabled { opacity: 0.5; cursor: default; }
	.confirm.compact { margin: 6px 0 2px; padding: 4px 4px 4px 10px; font-size: 0.75rem; }
	.confirm.compact .btn { padding: 3px 10px; }
	.inline-body { margin-top: 4px; padding-left: 4px; }
	.inline-body.capped {
		max-height: 15.5rem;
		overflow-y: auto;
		overscroll-behavior: contain;
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 0.8);
		padding: 2px 6px 4px;
	}
	.inline .group-head {
		display: flex;
		gap: 6px;
		margin: 6px 0 2px;
		font-size: 0.65625rem;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.inline .group-count { font-variant-numeric: tabular-nums; opacity: 0.8; }
	.inline .group:first-child .group-head { margin-top: 2px; }
	.inline-body.capped .group-head { position: sticky; top: 0; z-index: 1; background: var(--background); padding-top: 4px; margin-top: 0; }
	.size-compact.row .row-inner, .member.size-compact {
		grid-template-columns: 20px minmax(0, 1fr) auto auto;
		gap: 8px;
		min-height: 28px;
		background: transparent;
	}
	.size-compact .row-icon { width: 20px; height: 20px; }
	.size-compact .row-name { font-size: 0.75rem; }
	.size-compact .row-meta { flex-direction: row-reverse; gap: 6px; align-items: baseline; max-width: 14rem; font-size: 0.6875rem; }
	.size-compact .meta-worker::after { content: '·'; margin-left: 6px; opacity: 0.6; }
	.size-compact .meta-time { font-weight: 500; }
	.size-compact .icon-btn, .size-compact .icon-slot { width: 24px; height: 24px; }
	.size-compact .swarm-count { font-size: 0.625rem; }
	.size-compact .members { margin: 0 0 4px 10px; padding-left: 10px; }
	.size-compact .confirm.in-row { margin: 2px 0 4px 28px; padding: 4px 4px 4px 10px; font-size: 0.75rem; }
	.size-compact .confirm.in-row .btn { padding: 3px 10px; }

	/* ── Phone summary row (mobile shell inline) ───────────────────────────── */
	.summary-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		width: 100%;
		min-height: 52px;
		padding: 6px 6px 6px 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.summary-row:active { background: var(--hover-overlay); }
	.pulse { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 50%; color: var(--primary); background: color-mix(in oklch, var(--primary) 14%, transparent); }
	.summary-text { display: flex; flex-direction: column; min-width: 0; line-height: 1.3; }
	.summary-text b { font-size: 0.875rem; font-weight: 600; }
	.summary-text small { font-size: 0.75rem; color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.summary-open { display: grid; place-items: center; min-height: 40px; padding: 0 14px; border-radius: calc(var(--radius, 0.75rem) * 0.7); background: var(--muted); font-size: 0.8125rem; font-weight: 600; }

	/* ── Sheet ─────────────────────────────────────────────────────────────── */
	.rt-layer { position: fixed; inset: 0; z-index: 170; display: flex; align-items: center; justify-content: center; }
	.rt-scrim { position: absolute; inset: 0; background: var(--modal-overlay, rgb(0 0 0 / 0.5)); animation: rt-fade 0.18s ease; }
	.rt-sheet {
		position: relative;
		display: flex;
		flex-direction: column;
		width: min(560px, calc(100vw - 32px));
		height: min(720px, calc(100dvh - 64px));
		background: var(--background);
		color: var(--foreground);
		border: 1px solid var(--border-strong, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 1.3);
		box-shadow: var(--shadow-l);
		overflow: hidden;
		outline: none;
		animation: rt-pop 0.2s cubic-bezier(0.2, 0.7, 0.2, 1);
	}
	.rt-head {
		flex: none;
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 6px 12px 10px 6px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		background: var(--background);
	}
	.rt-top { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; align-items: center; gap: 6px; }
	.back { width: 44px; height: 44px; color: var(--foreground); }
	.rt-sheet :global(.back-arrow) { display: none; }
	.rt-title { min-width: 0; }
	.rt-title h2 { margin: 0; font-size: 1rem; font-weight: 600; line-height: 1.3; }
	.rt-title p { margin: 0; font-size: 0.75rem; color: var(--muted-foreground); font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	/* Compact pill; the ::before extends the hit area to 44px. */
	.stop-all {
		position: relative;
		height: 32px;
		padding: 0 12px;
		border-radius: 999px;
		border: 1px solid color-mix(in oklch, var(--destructive) 45%, transparent);
		background: transparent;
		color: var(--destructive);
		font: inherit;
		font-size: 0.8125rem;
		font-weight: 600;
		white-space: nowrap;
		cursor: pointer;
	}
	.stop-all::before { content: ''; position: absolute; inset: -6px -2px; }
	.stop-all:hover:not(:disabled) { background: color-mix(in oklch, var(--destructive) 10%, transparent); }
	.stop-all:disabled { opacity: 0.45; cursor: not-allowed; }
	.rt-head .confirm { margin-left: 6px; padding: 6px 6px 6px 12px; font-size: 0.875rem; }
	.rt-head .confirm .btn { min-height: 40px; padding: 0 14px; font-size: 0.8125rem; }
	.rt-search {
		display: flex;
		align-items: center;
		gap: 8px;
		min-height: 44px;
		margin-left: 6px;
		padding: 0 6px 0 12px;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border);
		background: var(--card);
		color: var(--muted-foreground);
	}
	.rt-search:focus-within { border-color: var(--ring); }
	.rt-search input { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--foreground); font: inherit; font-size: 0.9375rem; }
	.rt-search input::-webkit-search-cancel-button { display: none; }
	.clear { display: grid; place-items: center; width: 32px; height: 32px; border: 0; border-radius: 50%; background: transparent; color: var(--muted-foreground); cursor: pointer; }
	.clear:hover { background: var(--hover-overlay); color: var(--foreground); }
	/* Three equal columns: never overflow, label and count never run together. */
	.rt-chips {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 4px;
		margin-left: 6px;
		padding: 3px;
		border-radius: var(--radius, 0.75rem);
		background: var(--card);
		border: 1px solid var(--border-subtle, var(--border));
	}
	/* With Watches, columns size to their labels first: at 320px "All" gives
	   "Watches" the room it needs instead of four equal, truncated cells. */
	.rt-chips.four { grid-template-columns: repeat(4, minmax(max-content, 1fr)); }
	.rt-chips.four .chip { padding: 0 4px; gap: 4px; }
	.chip {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		min-width: 0;
		min-height: 38px;
		padding: 0 6px;
		border: 0;
		border-radius: calc(var(--radius, 0.75rem) * 0.7);
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 0.8125rem;
		cursor: pointer;
	}
	.chip:hover { color: var(--foreground); }
	.chip[aria-pressed='true'] { background: var(--accent); color: var(--foreground); font-weight: 600; box-shadow: inset 0 0 0 1px var(--border-strong, var(--border)); }
	.chip-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.chip-count { flex: none; min-width: 1.5em; padding: 0 5px; border-radius: 999px; background: var(--muted); font-size: 0.6875rem; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.6; text-align: center; }
	.chip[aria-pressed='true'] .chip-count { background: color-mix(in oklch, var(--primary) 18%, transparent); color: var(--primary); }
	.rt-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding-bottom: calc(16px + env(safe-area-inset-bottom, 0px)); }
	.rt-warn { margin: 8px 16px 0; font-size: 0.75rem; color: var(--warning); }
	.link { border: 0; background: transparent; color: var(--primary); font: inherit; font-weight: 600; cursor: pointer; padding: 0; text-decoration: underline; }
	.finished { border-top: 1px solid var(--border-subtle, var(--border)); }
	.finished summary {
		display: flex;
		align-items: center;
		gap: 8px;
		min-height: 44px;
		padding: 0 16px;
		list-style: none;
		cursor: pointer;
		font-size: 0.78125rem;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.finished summary::-webkit-details-marker { display: none; }
	.finished summary:hover { color: var(--foreground); }
	.finished summary:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.finished :global(.finished-chev) { transform: rotate(-90deg); transition: transform 0.15s ease; }
	.finished[open] :global(.finished-chev) { transform: none; }
	.finished-body { padding: 0 10px 12px; }
	.rt-empty { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 48px 24px; color: var(--muted-foreground); text-align: center; font-size: 0.875rem; }
	.rt-empty p { margin: 0; overflow-wrap: anywhere; }
	.rt-empty :global(svg) { color: var(--success); }
	.rt-list .group-head {
		position: sticky;
		top: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0;
		padding: 10px 16px 6px;
		background: var(--background);
		border-bottom: 1px solid var(--border-subtle, var(--border));
		font-size: 0.78125rem;
		font-weight: 600;
		color: var(--foreground);
	}
	.rt-list .group-count { color: var(--muted-foreground); font-variant-numeric: tabular-nums; }
	.size-roomy.row { overflow: hidden; border-bottom: 1px solid var(--border-subtle, var(--border)); }
	.row-under {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 8px;
		padding-right: 20px;
		background: color-mix(in oklch, var(--destructive) 14%, var(--background));
		color: var(--destructive);
		font-size: 0.8125rem;
		font-weight: 600;
		max-height: 56px;
	}
	.row.armed .row-under { background: var(--destructive); color: var(--destructive-foreground); }
	.size-roomy .row-inner, .member.size-roomy {
		grid-template-columns: 28px minmax(0, 1fr) auto auto;
		gap: 10px;
		min-height: 56px;
		padding: 0 6px 0 14px;
		touch-action: pan-y;
		transition: transform 0.22s cubic-bezier(0.22, 1, 0.36, 1);
	}
	.size-roomy .row-inner.dragging { transition: none; }
	.size-roomy .row-icon { width: 28px; height: 28px; }
	.size-roomy .row-name { font-size: 0.875rem; min-height: 44px; }
	.size-roomy .row-name.mono .name-text { font-size: 0.8125rem; }
	.size-roomy .row-meta { font-size: 0.71875rem; max-width: 6.5rem; }
	.size-roomy .meta-time { font-size: 0.75rem; }
	.size-roomy .icon-btn, .size-roomy .icon-slot { width: 44px; height: 44px; }
	.size-roomy .members { margin: 0 0 6px 27px; }
	.member.size-roomy { min-height: 48px; padding-left: 12px; grid-template-columns: 16px minmax(0, 1fr) auto auto; }
	.member.size-roomy .row-name { font-size: 0.8125rem; font-weight: 500; }
	/* On a phone the member's title gets the row; its agent is in the tooltip. */
	@media (max-width: 520px) {
		.member.size-roomy .member-agent { display: none; }
	}
	.member-empty { padding: 10px 12px; font-size: 0.8125rem; }
	.size-roomy .confirm.in-row { margin: 0 12px 10px 52px; padding: 6px 6px 6px 12px; font-size: 0.8125rem; }
	.size-roomy .confirm.in-row .btn { min-height: 40px; padding: 0 12px; }

	/* ── Watches ───────────────────────────────────────────────────────────── */
	/* Compact rows keep the task grid: name and meta are its cells. */
	.watch-text { display: contents; }
	.state-chip {
		flex: none;
		font-size: 0.6875rem;
		font-weight: 600;
		line-height: 1.5;
		padding: 0 6px;
		border-radius: 999px;
		/* Mixed toward the text colour so small type passes AA on light themes. */
		color: color-mix(in oklch, var(--warning) 72%, var(--foreground));
		background: color-mix(in oklch, var(--warning) 14%, transparent);
	}
	.size-compact .state-chip { font-size: 0.625rem; padding: 0 5px; }
	.watch.is-paused .row-icon { color: var(--muted-foreground); }
	.watch.is-paused .meta-time { color: var(--muted-foreground); font-weight: 500; }
	.meta-cost { font-variant-numeric: tabular-nums; }
	.icon-btn.resume { color: var(--primary); }
	.watch-mark { display: grid; place-items: center; flex: none; width: 12px; height: 12px; color: var(--primary); }
	.pulse.quiet { color: var(--muted-foreground); background: var(--muted); }
	/* Roomy rows (the sheet, phones): name over "Fired 5m ago · $0.42", so
	   three 44px actions still leave the name room at 320px. */
	.size-roomy.watch .row-inner {
		grid-template-columns: 28px minmax(0, 1fr) auto;
		padding-top: 6px;
		padding-bottom: 6px;
	}
	.size-roomy .watch-text { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
	.size-roomy.watch .row-name { min-height: 0; }
	.size-roomy.watch .name-text {
		white-space: normal;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		line-height: 1.3;
		overflow-wrap: anywhere;
	}
	.size-roomy.watch .row-meta { flex-direction: row; align-items: baseline; gap: 6px; max-width: none; text-align: left; }
	.size-roomy.watch .meta-time { font-size: 0.75rem; font-weight: 500; color: var(--muted-foreground); }
	.size-roomy.watch .row-meta .state-chip { align-self: center; }
	.state-chip.on-name.roomy { display: none; }
	/* The cost never truncates; "Fired 2d ago" gives way first. */
	.size-roomy.watch .meta-cost { flex: none; max-width: none; overflow: visible; }
	.size-roomy.watch .meta-time { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.size-roomy.watch .meta-cost::before { content: '·'; margin-right: 6px; opacity: 0.6; }
	.size-roomy.watch .paused-note { margin: -2px 12px 8px 52px; }
	/* Narrow phones: the name takes the full width (two lines); the meta line
	   and the three 44px actions share the line under it. */
	@media (max-width: 480px) {
		.size-roomy.watch .row-inner {
			grid-template-columns: 28px minmax(0, 1fr) auto;
			grid-template-areas: 'icon name name' 'icon meta actions';
			row-gap: 0;
			padding-top: 8px;
			padding-bottom: 0;
		}
		.size-roomy .watch-text { display: contents; }
		.size-roomy.watch .row-icon { grid-area: icon; align-self: start; }
		.size-roomy.watch .row-name { grid-area: name; }
		.size-roomy.watch .row-meta { grid-area: meta; }
		.size-roomy.watch .row-actions { grid-area: actions; }
		.size-roomy.watch .paused-note { margin-top: 0; }
		.state-chip.on-name.roomy { display: inline; }
		.size-roomy.watch .row-meta .state-chip.on-meta { display: none; }
		/* "3h ago · $1.87" fits beside three actions at 320px; screen readers
		   still hear "Fired". */
		.size-roomy.watch .fired-word {
			position: absolute;
			width: 1px;
			height: 1px;
			overflow: hidden;
			clip-path: inset(50%);
			white-space: nowrap;
		}
	}
	/* A phone-width browser on the web shell: same idea for compact rows. */
	@media (max-width: 520px) {
		.size-compact.watch .row-inner {
			grid-template-areas: 'icon name name' '. meta actions';
			row-gap: 0;
			padding-top: 2px;
		}
		.size-compact.watch .row-icon { grid-area: icon; }
		.size-compact.watch .row-name { grid-area: name; }
		.size-compact.watch .row-meta { grid-area: meta; justify-self: start; }
		.size-compact.watch .row-actions { grid-area: actions; }
	}
	.watch .paused-note { color: color-mix(in oklch, var(--warning) 72%, var(--foreground)); }

	/* Phone: the sheet is a full-height page. */
	.rt-layer.mobile { align-items: stretch; justify-content: stretch; }
	.rt-layer.mobile .rt-sheet {
		width: 100%;
		height: 100%;
		border: 0;
		border-radius: 0;
		padding-top: env(safe-area-inset-top, 0px);
		animation: rt-slide 0.28s cubic-bezier(0.22, 1, 0.36, 1);
	}
	.rt-layer.mobile .rt-sheet :global(.back-arrow) { display: block; }
	.rt-layer.mobile .rt-sheet :global(.back-x) { display: none; }
	@media (max-width: 700px) {
		.rt-layer { align-items: stretch; justify-content: stretch; }
		.rt-sheet { width: 100%; height: 100%; border: 0; border-radius: 0; padding-top: env(safe-area-inset-top, 0px); animation: rt-slide 0.28s cubic-bezier(0.22, 1, 0.36, 1); }
		.rt-sheet :global(.back-arrow) { display: block; }
		.rt-sheet :global(.back-x) { display: none; }
	}

	@keyframes rt-fade { from { opacity: 0; } to { opacity: 1; } }
	@keyframes rt-pop { from { opacity: 0; transform: translateY(8px) scale(0.985); } to { opacity: 1; transform: none; } }
	@keyframes rt-slide { from { transform: translateX(24px); opacity: 0; } to { transform: none; opacity: 1; } }

	@media (prefers-reduced-motion: reduce) {
		.rt-scrim, .rt-sheet, .rt-layer.mobile .rt-sheet { animation: none; }
		.size-roomy .row-inner, .inline-toggle, .icon-btn, .row :global(.swarm-chev), .inline :global(.inline-chev) { transition: none; }
		.spinner, .icon-btn :global(.spin), .member[data-status='running'] .member-dot { animation-duration: 2.4s; }
	}
</style>
