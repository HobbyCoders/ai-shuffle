<script lang="ts">
	/**
	 * ProjectWorkersTab — the project's worker table (GET /projects/workers):
	 * one compact row per approved worker with its status, folder, branch and
	 * when it was last seen. Status carries the row's own action: Retry for a
	 * failed setup, Set up (or Set up when online) where the project is missing.
	 *
	 * Selecting a row opens that worker's inspector (beside the table; a sheet
	 * on a phone, where the table is a list), in the states of the mockup:
	 *
	 *   ready           folder with Change folder, branch · clean / N changed files, New chat here, Remove
	 *   missing         editable folder (suggested), what a setup does, Set up, Use a folder already there…
	 *   setting up      progress (indeterminate when unknown), "You can leave this page", Cancel
	 *   failed          the plain reason, the fix for its code, Retry (folder stays editable)
	 *   offline         "Set up when online" (queues it) / "Waiting for <worker>" with Withdraw
	 *
	 * This worker's removal is the local one (ProjectLocalRemoval); another
	 * worker's counts its chats with a fresh read before Remove is offered.
	 *
	 * Above the table, a banner names the online workers that lack the project
	 * with "Set up on all missing". While any setup is queued or running the
	 * list is re-read every 2 s; the `projects` resources_changed signal also
	 * refreshes it. Owner/admin only actions; API users never see folders.
	 */
	import { onDestroy, tick, untrack } from 'svelte';
	import { ChevronRight, MessageSquarePlus, MonitorSmartphone, RotateCw, X } from 'lucide-svelte';
	import { RowGroup, SettingRow, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import { workerDisplayName } from '$lib/components/ui/worker-name';
	import './project-tabs.css';
	import { removalCheckError, removalConfirmText } from './project-removal';
	import ProjectLocalRemoval from './ProjectLocalRemoval.svelte';
	import ProjectSheet from './ProjectSheet.svelte';
	import {
		browseWorkerFolder,
		cancelProjectSetup,
		changeWorkerFolder,
		getProjectWorkers,
		isUnsupported,
		removeProjectFromWorker,
		startProjectSetup,
		type ProjectRef,
		type ProjectWorkerDetail,
		type ProjectWorkersResponse
	} from '$lib/api/project-workers';
	import { watchResources } from '$lib/services/watch-resources';
	import { activeSession } from '$lib/stores/active-session';
	import { appData } from '$lib/stores/app-data.svelte';
	import { patchOverviewWorker } from '$lib/stores/projects-overview';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { SETUP_POLL_MS } from '$lib/stores/project-setup';
	import {
		errorAction,
		failureReason,
		hasProject,
		heldByNoWorker,
		isRunning,
		middleTruncate,
		seenAgo,
		workerStatus
	} from './project-workers-view';

	interface Props {
		projectRef: ProjectRef;
		projectName: string;
		/** This worker's legacy id for the project, when it is here. */
		localProjectId: string | null;
		/** The worker whose inspector is open (a header chip or the parent can set it). */
		selectedInstallId?: string | null;
		/** Phone layout: the table is a list and the inspector a sheet. */
		stacked?: boolean;
		/** The project's workers changed (a setup started, finished or was removed). */
		onChanged?: () => void;
		/** Each answer of the worker list (the parent learns this worker's row from it). */
		onLoaded?: (res: ProjectWorkersResponse) => void;
		/**
		 * The first read found no worker list for this project (an older worker, or
		 * no shared identity yet), or a list that belongs to another checkout (see
		 * `expectLocalId`).
		 */
		onUnavailable?: () => void;
		/**
		 * Read by local id (`{project_id}`): the list must name this local project.
		 * Two checkouts of one origin share a logical project, and the answer then
		 * describes the other one; its folder controls would change the wrong
		 * registration, so such an answer shows nothing and `onUnavailable` runs.
		 */
		expectLocalId?: string | null;
		/** Project settings sent the owner here: open this worker's removal confirmation in its inspector. */
		askSelfRemoval?: boolean;
		/** The request above was taken (so a later remount does not ask again). */
		onSelfRemovalAsked?: () => void;
	}

	let { projectRef, projectName, localProjectId, selectedInstallId = $bindable(null), stacked = false, onChanged, onLoaded, onUnavailable, expectLocalId = null, askSelfRemoval = false, onSelfRemovalAsked }: Props = $props();

	let data = $state<ProjectWorkersResponse | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	/**
	 * The last action's failure. One started from a worker's inspector names that
	 * worker and shows there (on a phone the page banner sits behind its sheet);
	 * one started from the page (Set up on all missing) has no worker and shows on the page.
	 */
	let actionError = $state<{ installId: string | null; message: string } | null>(null);
	/**
	 * Project settings sent the owner to this worker's removal: its confirmation
	 * opens at once. A new id per request, so asking again after Cancel asks again.
	 */
	let askLocal = $state(0);
	let askLocalSeq = 0;
	/**
	 * This worker's removal confirmation is open. Held here, above the
	 * aside/phone-sheet switch, so resizing across the breakpoint keeps it open
	 * (the removal row remounts); cleared with every other removal state.
	 * Its running request and failure are held here too: a remount mid-DELETE
	 * keeps Remove disabled, and the failure lands in the row on screen. The
	 * request alone clears `localRemoving`, so nothing can send a second one.
	 */
	let localConfirming = $state(false);
	let localRemoving = $state(false);
	let localRemovalError = $state<string | null>(null);
	/** Folder field per worker (seeded from the suggestion, then the owner's edits). */
	let folders = $state<Record<string, string>>({});
	/** Workers whose inspector is in "use a folder already there" mode. */
	let adopting = $state<Record<string, boolean>>({});
	/** Request in flight per worker (set up, cancel, remove). */
	let busy = $state<Record<string, boolean>>({});
	let editing = $state<{ installId: string; revision: string; folder: string; error: string | null; browsing: boolean } | null>(null);
	let confirmingRemove = $state<string | null>(null);
	/**
	 * The open removal confirmation's own fresh read of that worker. Never the
	 * cached detail: a chat created since would make its count wrong, so the
	 * count stays hidden and Remove disabled until this answers.
	 */
	let removalCheck = $state<{ installId: string; detail: ProjectWorkerDetail | null; error: string | null } | null>(null);
	let removalAsk = 0;
	let githubHelp = $state<string | null>(null);
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let seq = 0;
	let destroyed = false;
	let lastSignature = '';

	const refKey = $derived(JSON.stringify(projectRef));
	const kind = $derived(data?.kind ?? null);
	const workers = $derived((data?.workers ?? []).map(w => ({ ...w, device_name: workerDisplayName(w.device_name, w.is_self ? 'Local' : 'Worker') })));
	const missingOnline = $derived(workers.filter((w) => w.online && (w.state === 'missing') && !isRunning(w)));
	const anyRunning = $derived(workers.some((w) => isRunning(w)));
	const orphaned = $derived(heldByNoWorker(workers));
	const selected = $derived(workers.find((w) => w.install_id === selectedInstallId) ?? null);
	const inspecting = $derived(!!selected && !stacked);

	function nameList(names: string[]): string {
		if (names.length <= 1) return names[0] ?? '';
		if (names.length === 2) return `${names[0]} and ${names[1]}`;
		return `${names.length} workers`;
	}

	async function load() {
		const mine = ++seq;
		try {
			const res = await getProjectWorkers(projectRef);
			if (mine !== seq || destroyed) return;
			// `local_project_id` is in the answer but not in its client type (read-only here).
			if (expectLocalId && (res as ProjectWorkersResponse & { local_project_id?: string | null }).local_project_id !== expectLocalId) {
				data = null;
				onUnavailable?.();
				return;
			}
			// A setup on this worker just finished: reload its catalog so the
			// project's local tabs unlock without waiting for the live-sync push.
			const selfWas = data?.workers.find((w) => w.is_self)?.state;
			const selfNow = res.workers.find((w) => w.is_self)?.state;
			const selfArrived = !!data && selfWas !== 'ready' && selfNow === 'ready';
			data = res;
			loadError = null;
			onLoaded?.(res);
			if (selfArrived) void appData.loadProjectCatalog(performance.now());
			// Keep the list/hero chips in step with what this tab sees.
			const signature = res.workers.map((w) => `${w.install_id}:${w.state}:${w.setup?.status ?? ''}`).join('|');
			if (signature !== lastSignature) {
				lastSignature = signature;
				onChanged?.();
			}
			const next = { ...folders };
			for (const w of res.workers) {
				if (next[w.install_id] === undefined) next[w.install_id] = w.suggested_folder || res.default_folder || '';
			}
			folders = next;
		} catch (e) {
			if (mine !== seq || destroyed) return;
			// No list at all for this project: the parent offers its own fallback instead.
			if (!data && onUnavailable && isUnsupported(e)) { onUnavailable(); return; }
			const err = e as { detail?: string; message?: string };
			loadError = err.detail || err.message || "Couldn't load this project's workers.";
		} finally {
			if (mine === seq) loading = false;
			schedulePoll();
		}
	}

	function schedulePoll() {
		if (pollTimer) clearTimeout(pollTimer);
		pollTimer = undefined;
		if (destroyed || !anyRunning) return;
		pollTimer = setTimeout(() => void load(), SETUP_POLL_MS);
	}

	// Load on mount and whenever the project changes.
	// Keyed on the ref's value only: the parent hands a fresh object on every
	// render, and the load must not track it (or each refresh would restart it).
	$effect(() => {
		void refKey;
		untrack(() => {
			loading = true;
			data = null;
			folders = {};
			lastSignature = '';
			editing = null;
			busy = {};
			cancelRemove();
			void load();
		});
	});
	// Keep polling while something runs (the answer after a start may arrive first).
	$effect(() => {
		if (anyRunning && !pollTimer) schedulePoll();
	});
	watchResources(['projects'], () => load());
	onDestroy(() => {
		destroyed = true;
		if (pollTimer) clearTimeout(pollTimer);
	});

	// A worker was selected (a row, a header chip): bring its inspector into
	// view and focus it, once per selection (not on every poll, which would
	// pull focus out of a folder field).
	let focusedFor: string | null = null;
	const loaded = $derived(!!data);
	$effect(() => {
		const id = selectedInstallId;
		if (!id || !loaded) { if (!id) focusedFor = null; return; }
		if (focusedFor === id) return;
		focusedFor = id;
		void tick().then(() => {
			const el = document.getElementById(`pw-card-${id}`);
			// Beside the table it is already in view; below it (narrow panes) bring its top up.
			const top = el?.getBoundingClientRect().top ?? 0;
			if (top < 0 || top > window.innerHeight - 80) el?.scrollIntoView?.({ block: 'start', behavior: 'smooth' });
			el?.focus({ preventScroll: true });
		});
	});

	// Settings' "Remove from <this worker>": once the list is here, open this
	// worker's inspector with its removal confirmation already asking.
	$effect(() => {
		if (!askSelfRemoval || !data) return;
		const self = data.workers.find((w) => w.is_self);
		untrack(() => {
			onSelfRemovalAsked?.();
			if (!self) return;
			editing = null;
			cancelRemove();
			selectedInstallId = self.install_id;
			askLocal = ++askLocalSeq;
		});
	});

	function select(id: string | null) {
		if (id === selectedInstallId) return;
		const previous = selectedInstallId;
		editing = null;
		askLocal = 0;
		cancelRemove();
		if (id) leftSheet = false;
		selectedInstallId = id;
		// Closing the inspector hands focus back to the row that opened it.
		if (!id && previous) void tick().then(() => document.getElementById(`pw-row-${previous}`)?.focus());
	}

	function errMessage(e: unknown, fallback: string): string {
		const err = e as { detail?: unknown; message?: string };
		return typeof err.detail === 'string' ? err.detail : err.message || fallback;
	}

	/** Record a failed action: on its worker's inspector, or on the page when the page started it. */
	function failed(installId: string | null, message: string) {
		actionError = { installId, message };
	}

	async function setUp(w: ProjectWorkerDetail, mode: 'auto' | 'adopt' = 'auto', fromPage = false) {
		busy = { ...busy, [w.install_id]: true };
		actionError = null;
		try {
			const folder = (folders[w.install_id] ?? '').trim();
			const res = await startProjectSetup(projectRef, { install_id: w.install_id, folder: folder || undefined, mode });
			// Show it at once; the next read replaces this with the server's answer.
			const state = res.status === 'done' ? 'ready' : res.status === 'queued_offline' ? 'queued_offline' : 'setting_up';
			patchWorker(w.install_id, {
				state,
				setup: { op_id: res.op_id, status: res.status === 'done' ? 'done' : res.status, progress: null }
			});
			if (data) patchOverviewWorker(data.project_v2_id, w.install_id, { state, setup: { op_id: res.op_id, status: res.status, progress: null } });
			adopting = { ...adopting, [w.install_id]: false };
			onChanged?.();
			void load();
		} catch (e) {
			failed(fromPage ? null : w.install_id, `Couldn't start the setup on ${w.device_name}: ${errMessage(e, 'unknown error')}`);
		} finally {
			busy = { ...busy, [w.install_id]: false };
		}
	}

	async function setUpAllMissing() {
		await Promise.all(missingOnline.map((w) => setUp(w, 'auto', true)));
	}

	async function cancel(w: ProjectWorkerDetail) {
		const opId = w.setup?.op_id;
		if (!opId) return;
		busy = { ...busy, [w.install_id]: true };
		actionError = null;
		try {
			await cancelProjectSetup(opId);
		} catch (e) {
			// 409: it already finished; the reload shows how.
			if ((e as { status?: number }).status !== 409) failed(w.install_id, `Couldn't cancel it on ${w.device_name}: ${errMessage(e, 'unknown error')}`);
		} finally {
			busy = { ...busy, [w.install_id]: false };
			onChanged?.();
			void load();
		}
	}

	async function askRemove(w: ProjectWorkerDetail) {
		const ask = ++removalAsk;
		const installId = w.install_id;
		confirmingRemove = installId;
		removalCheck = { installId, detail: null, error: null };
		try {
			const res = await getProjectWorkers(projectRef);
			if (ask !== removalAsk || destroyed) return;
			const fresh = res.workers.find((x) => x.install_id === installId) ?? null;
			const error = removalCheckError(fresh, w.device_name);
			removalCheck = error ? { installId, detail: null, error } : { installId, detail: fresh, error: null };
		} catch (e) {
			if (ask !== removalAsk || destroyed) return;
			removalCheck = { installId, detail: null, error: `Couldn't count the chats this removes on ${w.device_name}: ${errMessage(e, 'unknown error')}` };
		}
	}

	function cancelRemove() {
		removalAsk++;
		confirmingRemove = null;
		removalCheck = null;
		localConfirming = false;
		localRemovalError = null;
	}

	async function remove(w: ProjectWorkerDetail) {
		busy = { ...busy, [w.install_id]: true };
		actionError = null;
		try {
			await removeProjectFromWorker(w.install_id, projectRef);
			cancelRemove();
			if (w.is_self) await appData.loadProjectCatalog(performance.now());
			onChanged?.();
			void load();
		} catch (e) {
			failed(w.install_id, `Couldn't remove it from ${w.device_name}: ${errMessage(e, 'unknown error')}`);
		} finally {
			busy = { ...busy, [w.install_id]: false };
		}
	}

	function localRemoved() {
		onChanged?.();
		void load();
	}

	function openFolderEditor(w: ProjectWorkerDetail) {
		if (!w.folder_revision || !w.folder || !w.online || loadError) return;
		cancelRemove();
		editing = { installId: w.install_id, revision: w.folder_revision, folder: w.folder, error: null, browsing: false };
		void tick().then(() => {
			const input = document.getElementById(`pw-card-${w.install_id}`)?.querySelector<HTMLInputElement>('[data-change-folder-input] input');
			input?.focus();
			input?.select();
		});
	}

	function closeFolderEditor(id: string) {
		editing = null;
		void tick().then(() => document.getElementById(`pw-change-${id}`)?.focus());
	}

	async function saveFolder(w: ProjectWorkerDetail) {
		const draft = editing;
		if (!draft || !w.online || w.state !== 'ready' || loadError || busy[w.install_id]
			|| w.folder_revision !== draft.revision || !draft.folder.trim() || draft.folder.trim() === w.folder) return;
		busy = { ...busy, [w.install_id]: true };
		draft.error = null;
		const projectKey = refKey;
		try {
			const result = await changeWorkerFolder(w.install_id, projectRef, draft.folder.trim(), draft.revision);
			if (destroyed || projectKey !== refKey) return;
			++seq; // A read started before the write must not restore the old folder.
			patchWorker(w.install_id, { ...result, branch: null, dirty: null, changed_files: null });
			if (editing === draft) closeFolderEditor(w.install_id);
			if (w.is_self) void appData.loadProjectCatalog(performance.now());
			onChanged?.();
			void load();
		} catch (e) {
			if (destroyed || projectKey !== refKey || editing !== draft) return;
			draft.error = errMessage(e, "Couldn't change the folder. Check the current folder before retrying.");
			void load();
		} finally {
			if (!destroyed && projectKey === refKey) busy = { ...busy, [w.install_id]: false };
		}
	}

	function patchWorker(id: string, patch: Partial<ProjectWorkerDetail>) {
		if (!data) return;
		data = { ...data, workers: data.workers.map((w) => (w.install_id === id ? { ...w, ...patch } : w)) };
	}

	/**
	 * Before an inspector action opens something outside the Projects page: on a
	 * phone the inspector is a sheet above everything, so close it, or what opens
	 * sits behind it. Focus goes with what opens, not back to the row (unlike select).
	 */
	let leftSheet = false;
	const sheetReturnsFocus = () => !leftSheet;
	function leaveSheet() {
		if (!stacked) return;
		leftSheet = true;
		editing = null;
		askLocal = 0;
		cancelRemove();
		selectedInstallId = null;
	}

	function newChatHere(w: ProjectWorkerDetail) {
		leaveSheet();
		activeSession.newChat();
		activeSession.setProject(localProjectId ?? '', data?.project_v2_id ?? null, { user: true });
		activeSession.setDeviceIntent(w.is_self ? null : { id: w.install_id, name: w.device_name });
		layout.openChat(null);
	}

	function focusFolder(id: string) {
		const input = document.getElementById(`pw-folder-${id}`)?.querySelector<HTMLInputElement>('input');
		input?.focus();
		input?.select();
	}

	function onErrorAction(w: ProjectWorkerDetail) {
		const action = errorAction(w.setup?.error_code, w.device_name);
		if (!action) return;
		if (action.kind === 'folder') return focusFolder(w.install_id);
		if (w.is_self) {
			leaveSheet();
			layout.openFeaturePage('settings', 'category=integrations&item=github');
		} else githubHelp = githubHelp === w.install_id ? null : w.install_id;
	}

	/** A row's own action (Retry, Set up): it opens the inspector too, so the result shows there. */
	function rowSetUp(e: MouseEvent, w: ProjectWorkerDetail) {
		e.stopPropagation();
		select(w.install_id);
		void setUp(w);
	}

	function branchLine(w: ProjectWorkerDetail): string | null {
		if (!w.branch) return null;
		if (w.dirty === false || w.changed_files === 0) return `${w.branch} · clean`;
		if (typeof w.changed_files === 'number') return `${w.branch} · ${w.changed_files} changed file${w.changed_files === 1 ? '' : 's'}`;
		if (w.dirty) return `${w.branch} · uncommitted changes`;
		return w.branch;
	}

	function setupHint(w: ProjectWorkerDetail): string {
		if (adopting[w.install_id]) {
			return `Point at a folder on ${w.device_name} that already holds this project. It's adopted as it is; nothing is overwritten.`;
		}
		if (kind === 'folder') return `Uses an existing ${data?.default_folder || projectName} folder there, or creates an empty one; files aren't copied.`;
		const remote = data?.remote ? data.remote : 'the repository';
		return `Clones ${remote} using ${w.device_name}'s own GitHub sign-in. Never overwrites an existing folder.`;
	}

	function presence(w: ProjectWorkerDetail): string {
		return [w.is_self ? 'This worker' : '', w.online ? 'Online' : ['Offline', seenAgo(w.last_seen)].filter(Boolean).join(' · ')].filter(Boolean).join(' · ');
	}

	function lastSeen(w: ProjectWorkerDetail): string {
		if (w.online) return 'Now';
		const ago = seenAgo(w.last_seen);
		return ago ? ago.replace(/^seen /, '').replace(/^just now$/, 'Just now') : '—';
	}

	/** The folder this worker has (or will have) for the project. */
	function folderOf(w: ProjectWorkerDetail): string | null {
		if (hasProject(w.state) || isRunning(w)) return w.folder || folders[w.install_id] || w.suggested_folder || null;
		return null;
	}

	const placeName = (w: ProjectWorkerDetail) => (w.is_self ? 'here' : `on ${w.device_name}`);
</script>

{#snippet statusCell(w: ProjectWorkerDetail)}
	{@const st = workerStatus(w, kind)}
	<span class="pw-status pw-status--{st.tone}"><i class="pw-dot" aria-hidden="true"></i><span>{st.label}</span></span>
{/snippet}

{#snippet rowAction(w: ProjectWorkerDetail)}
	{#if $isAdmin && !isRunning(w) && !busy[w.install_id]}
		{#if w.state === 'failed' && w.online}<button type="button" class="st-btn sm" onclick={(e) => rowSetUp(e, w)}><RotateCw />Retry</button>
		{:else if w.state === 'missing' && w.online}<button type="button" class="st-btn sm" onclick={(e) => rowSetUp(e, w)}>Set up</button>
		{:else if w.state === 'offline_missing'}<button type="button" class="st-btn sm" onclick={(e) => rowSetUp(e, w)}>Set up when online</button>{/if}
	{/if}
{/snippet}

{#snippet inspector(w: ProjectWorkerDetail)}
	{@const running = isRunning(w)}
	{@const has = hasProject(w.state)}
	{@const failed = w.state === 'failed'}
	{@const action = failed ? errorAction(w.setup?.error_code, w.device_name) : null}
	{@const check = confirmingRemove === w.install_id && removalCheck?.installId === w.install_id ? removalCheck : null}
	{@const fresh = check?.detail ?? null}
	<article id="pw-card-{w.install_id}" class="pw-card" tabindex="-1" aria-label={w.device_name} data-worker-state={w.state}>
		{#snippet statusDetail()}
			{#if running}
				{@const p = w.setup?.progress}
				<progress class="project-progress" max="100" value={typeof p === 'number' ? p : undefined} aria-label="Setting up on {w.device_name}"></progress>
				<p class="project-muted">{kind === 'folder' ? 'Setting it up' : 'Cloning'}{folderOf(w) ? ` into ${folderOf(w)}` : ''}. You can leave this page; setup keeps going on {w.device_name}.</p>
			{:else if w.state === 'queued_offline'}<p class="project-muted">It sets up when {w.device_name} comes back online.</p>
			{:else if w.state === 'unknown'}<p class="project-muted">{w.device_name} is online but didn't answer. It may still have this project.</p>
			{:else if failed}<p class="project-error">{failureReason(w)}</p>{/if}
		{/snippet}
		{#snippet removalDetail()}
			{#if check?.error}<p class="project-error" role="alert">{check.error}</p>
			{:else if fresh?.removal_worktrees?.length}
				<details class="kept-worktrees" data-kept-worktrees>
					<summary>Worktree folders that stay on disk on {w.device_name}</summary>
					<ul>{#each fresh.removal_worktrees as path (path)}<li class="st-mono">{path}</li>{/each}</ul>
				</details>
			{/if}
		{/snippet}
		{#if actionError && actionError.installId === w.install_id}
			<InlineBanner>{actionError.message}{#snippet actions()}<button class="st-btn" onclick={() => (actionError = null)}>Dismiss</button>{/snippet}</InlineBanner>
		{/if}
		<RowGroup title="Status">
			<SettingRow label={running ? (kind === 'folder' ? 'Setting up…' : 'Cloning…') : has ? 'Ready' : w.state === 'queued_offline' ? `Waiting for ${w.device_name}` : failed ? (kind === 'folder' ? "Couldn't set it up" : "Couldn't clone") : w.state === 'unknown' ? `Couldn't check ${w.device_name}` : `Not set up ${w.is_self ? 'on this worker' : 'here'}`}
				hint={presence(w)} wrapHint expanded={running || w.state === 'queued_offline' || w.state === 'unknown' || failed ? statusDetail : undefined}>
				{#if running && $isAdmin && w.setup?.op_id}<button class="st-btn" onclick={() => cancel(w)} disabled={busy[w.install_id]}>Cancel setup</button>
				{:else if has}
					{#if w.online}<button class="st-btn primary" onclick={() => newChatHere(w)} disabled={busy[w.install_id]}><MessageSquarePlus />New chat here</button>{/if}
				{:else if w.state === 'queued_offline' && $isAdmin && w.setup?.op_id}<button class="st-btn" onclick={() => cancel(w)} disabled={busy[w.install_id]}>Withdraw</button>
				{:else if w.state === 'unknown'}<button class="st-btn" onclick={() => load()}><RotateCw />Check again</button>
				{:else if !running && $isAdmin}
					{#if !w.online}<button class="st-btn" onclick={() => setUp(w)} disabled={busy[w.install_id]}>Set up when online</button>
					{:else if failed}
						{#if action}<button class="st-btn" onclick={() => onErrorAction(w)}>{action.label}</button>{/if}<button class="st-btn" onclick={() => setUp(w)} disabled={busy[w.install_id]}><RotateCw />Retry</button>
					{:else if adopting[w.install_id]}<button class="st-btn primary" onclick={() => setUp(w, 'adopt')} disabled={busy[w.install_id] || !(folders[w.install_id] ?? '').trim()}>Use this folder</button><button class="st-btn" onclick={() => (adopting = { ...adopting, [w.install_id]: false })}>Back</button>
					{:else}<button class="st-btn primary" onclick={() => setUp(w)} disabled={busy[w.install_id]}>Set up {placeName(w)}</button><button class="st-btn" onclick={() => { adopting = { ...adopting, [w.install_id]: true }; void tick().then(() => focusFolder(w.install_id)); }}>Use a folder already there…</button>{/if}
				{/if}
			</SettingRow>
			{#if !has && !running && w.state !== 'queued_offline' && w.state !== 'unknown'}
				{#if $isAdmin && w.online}
					<SettingRow label={adopting[w.install_id] ? `Folder already on ${w.device_name}` : `Folder on ${w.device_name}`} hint={setupHint(w)} wrapHint>
						{#snippet expanded()}
							<form id="pw-folder-{w.install_id}" onsubmit={(e) => { e.preventDefault(); setUp(w, adopting[w.install_id] ? 'adopt' : 'auto'); }}>
								<TextField onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); setUp(w, adopting[w.install_id] ? 'adopt' : 'auto'); } }} value={folders[w.install_id] ?? ''} oninput={(v) => (folders = { ...folders, [w.install_id]: v })} mono width="full" placeholder={w.suggested_folder ?? data?.default_folder} label={adopting[w.install_id] ? `Folder already on ${w.device_name}` : `Folder on ${w.device_name}`} disabled={busy[w.install_id]} />
							</form>
						{/snippet}
					</SettingRow>
				{:else if !w.online}<SettingRow label="Worker offline" wrapHint hint={`Turn ${w.device_name} on to set it up — or queue it and it runs when ${w.device_name} comes back.`} />{/if}
			{/if}
			{#if githubHelp === w.install_id}<SettingRow label="GitHub sign-in" wrapHint hint={`On ${w.device_name}, open AI Shuffle, go to Settings → Integrations → GitHub and sign in. Then press Retry here.`} />{/if}
		</RowGroup>
		{#if has || running}
			<RowGroup title="Folder">
				<SettingRow label="Folder" hint={folderOf(w) || 'Not reported'} hintMono wrapHint>
					{#if $isAdmin && has && editing?.installId !== w.install_id && (w.folder_revision || !w.online)}
						<button id={`pw-change-${w.install_id}`} class="st-btn" title={w.online ? `Choose another folder on ${w.device_name}` : `${w.device_name} is offline`} disabled={!!editing || !w.online || !w.folder_revision || busy[w.install_id] || !!loadError} onclick={() => openFolderEditor(w)}>Change folder</button>
					{/if}
				</SettingRow>
				{#if editing?.installId === w.install_id && $isAdmin}
					{@const draft = editing}
					{@const stale = w.online && w.state === 'ready' && w.folder_revision !== draft.revision}
					{@const unavailable = !w.online || w.state !== 'ready' || !!loadError}
					<SettingRow label="Change folder" wrapHint hint={`Choose an existing folder on ${w.device_name}. No files are moved or copied. New chats and the next turn in chats without worktrees use the new folder. Existing worktrees stay where they are; keep their original checkout in place.`}>
						{#snippet expanded()}
							<form class="flex flex-col gap-2" onsubmit={(event) => { event.preventDefault(); void saveFolder(w); }}>
								<div class="project-controls" data-change-folder-input>
									<TextField bind:value={draft.folder} width="full" mono pathDisplay label={`New folder on ${w.device_name}`} disabled={busy[w.install_id] || unavailable || stale} />
									<button type="button" class="st-btn" disabled={busy[w.install_id] || unavailable || stale} aria-expanded={draft.browsing} onclick={() => (draft.browsing = !draft.browsing)}>Browse</button>
								</div>
								{#if draft.browsing && !unavailable && !stale && !busy[w.install_id]}
									<DirectoryBrowser browse={(path) => browseWorkerFolder(w.install_id, projectRef, path)} selectedPath={draft.folder} onSelect={(path) => { draft.folder = path; draft.browsing = false; }} />
								{/if}
								{#if stale}<p class="project-error" role="alert">The registered folder changed. Cancel and reopen to edit the current folder.</p>
								{:else if unavailable}<p class="project-error" role="alert">This worker's folder is unavailable. Check its status before saving.</p>{/if}
								{#if draft.error}<p class="project-error" role="alert">{draft.error}</p>{/if}
								<div class="project-controls">
									<button type="button" class="st-btn" disabled={busy[w.install_id]} onclick={() => closeFolderEditor(w.install_id)}>Cancel</button>
									<button type="submit" class="st-btn primary" disabled={busy[w.install_id] || unavailable || stale || !draft.folder.trim() || draft.folder.trim() === w.folder}>{busy[w.install_id] ? 'Saving…' : 'Save folder'}</button>
								</div>
							</form>
						{/snippet}
					</SettingRow>
				{/if}
				{#if kind === 'git'}<SettingRow label="Branch" hint={branchLine(w) || (running ? 'Pending setup' : 'Not reported')} />{/if}
			</RowGroup>
		{/if}
		{#if has && $isAdmin && editing?.installId !== w.install_id && (w.is_self ? !!localProjectId : w.online)}
			<RowGroup title="Remove">
				{#if w.is_self && localProjectId}
					<ProjectLocalRemoval projectId={localProjectId} {projectName} where={w.device_name} ask={askLocal} onAsked={() => (askLocal = 0)} bind:confirming={localConfirming} bind:removing={localRemoving} bind:error={localRemovalError} onRemoved={localRemoved} />
				{:else}
					<SettingRow label="Remove project" wrapHint
						hint={confirmingRemove !== w.install_id ? `Removes ${projectName} and its chats from ${w.device_name}. Files stay on disk.`
							: fresh ? removalConfirmText(projectName, w.device_name, { chats: fresh.removal_chats ?? null, worktrees: fresh.removal_worktree_count ?? null })
							: check?.error ? `Remove ${projectName} from ${w.device_name}?` : 'Counting the chats this removes…'}
						expanded={check?.error || fresh?.removal_worktrees?.length ? removalDetail : undefined}>
						{#if confirmingRemove === w.install_id}<button class="st-btn" onclick={cancelRemove} disabled={busy[w.install_id]}>Cancel</button><button class="st-btn danger" onclick={() => remove(w)} disabled={busy[w.install_id] || !fresh}>{busy[w.install_id] ? 'Removing…' : 'Remove'}</button>
						{:else}<button class="st-btn danger" onclick={() => askRemove(w)}>Remove project and chats from {w.device_name}</button>{/if}
					</SettingRow>
				{/if}
			</RowGroup>
		{/if}
	</article>
{/snippet}

<div data-ui-project-workers={loading ? 'loading' : loadError ? 'error' : 'ready'}>
	{#if loading && !data}<p class="project-muted" role="status" aria-busy="true" aria-label="Loading workers">Loading workers…</p>
	{:else if loadError && !data}
		<InlineBanner>{loadError}{#snippet actions()}<button class="st-btn" onclick={() => { loading = true; void load(); }}>Try again</button>{/snippet}</InlineBanner>
	{:else if data}
		<!-- A worker's own failure shows in its open inspector (on a phone the page sits behind its sheet). -->
		{#if actionError && !(actionError.installId && actionError.installId === selected?.install_id)}<InlineBanner>{actionError.message}{#snippet actions()}<button class="st-btn" onclick={() => (actionError = null)}>Dismiss</button>{/snippet}</InlineBanner>{/if}
		{#if $isAdmin && orphaned}
			<InlineBanner tone="info">No worker has this project any more. You can take it off the Projects list from Project settings; its chats keep it and nothing on disk is touched.</InlineBanner>
		{/if}
		{#if $isAdmin && missingOnline.length > 0}
			{@const names = nameList(missingOnline.map(w => w.device_name))}
			{@const onlySelf = missingOnline.length === 1 && missingOnline[0].is_self}
			<InlineBanner tone="info">{#if onlySelf}{projectName} isn't on {names} yet. Set it up here to start chats here and edit its MCP servers, agents, hooks and memory.{:else}{names} {missingOnline.length === 1 ? "doesn't" : "don't"} have this project yet. Set it up there to start chats on {names} too.{/if}
				{#snippet actions()}<button class="st-btn" onclick={setUpAllMissing} disabled={missingOnline.some(w => busy[w.install_id])}>{onlySelf ? 'Set up here' : 'Set up on all missing'}</button>{/snippet}
			</InlineBanner>
		{/if}
		{#if !workers.length}<EmptyDetail title="No other workers" message="Pair a worker in Settings to run this project there too." />
		{:else if stacked}
			<ul class="pw-list" aria-label="Workers">
				{#each workers as w (w.install_id)}
					<li data-worker-state={w.state}>
						<button id="pw-row-{w.install_id}" type="button" class="pw-list-row" aria-haspopup="dialog" onclick={() => select(w.install_id)}>
							<MonitorSmartphone class="pw-list-icon" aria-hidden="true" />
							<span class="pw-list-main">
								<span class="pw-list-name">{w.device_name}{#if w.is_self}<span class="pw-tag">This worker</span>{/if}</span>
								<span class="pw-list-sub" class:st-mono={!!folderOf(w)}>{folderOf(w) ? middleTruncate(folderOf(w) ?? '', 34) : presence(w)}</span>
							</span>
							{@render statusCell(w)}
							<ChevronRight class="pw-list-chev" aria-hidden="true" />
						</button>
					</li>
				{/each}
			</ul>
			{#if selected}
				<ProjectSheet steady title={selected.device_name} subtitle={presence(selected)} initial=".pw-card" returnFocus={sheetReturnsFocus} onClose={() => select(null)}>
					{@render inspector(selected)}
				</ProjectSheet>
			{/if}
		{:else}
			<div class="pw-layout" class:inspecting>
				<div class="pw-table-wrap">
					<table class="pw-table" aria-label="Workers">
						<thead>
							<tr>
								<th scope="col">Worker</th>
								<th scope="col">Status</th>
								<th scope="col">Folder</th>
								{#if kind === 'git' && !inspecting}<th scope="col">Branch</th>{/if}
								{#if !inspecting}<th scope="col">Last seen</th>{/if}
							</tr>
						</thead>
						<tbody>
							{#each workers as w (w.install_id)}
								{@const folder = folderOf(w)}
								<!-- The row is a large click target; its first cell's button is the keyboard path. -->
								<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
								<tr class:selected={w.install_id === selectedInstallId} data-worker-state={w.state} onclick={() => select(w.install_id)}>
									<td>
										<button id="pw-row-{w.install_id}" type="button" class="pw-row-btn" aria-pressed={w.install_id === selectedInstallId} aria-label="{w.device_name}: open its details" onclick={(e) => { e.stopPropagation(); select(w.install_id); }}>
											<span class="pw-row-name">{w.device_name}</span>
										</button>
										<span class="pw-row-sub">{[w.is_self ? 'This worker' : '', w.online ? 'Online' : 'Offline'].filter(Boolean).join(' · ')}</span>
									</td>
									<td><div class="pw-cell-status">{@render statusCell(w)}{@render rowAction(w)}</div></td>
									<td class="pw-cell-folder st-mono" title={folder ?? undefined}>{folder ? middleTruncate(folder, inspecting ? 28 : 40) : '—'}</td>
									{#if kind === 'git' && !inspecting}<td class="pw-cell-branch">{branchLine(w) ?? '—'}</td>{/if}
									{#if !inspecting}<td class="pw-cell-seen">{lastSeen(w)}</td>{/if}
								</tr>
							{/each}
						</tbody>
					</table>
					<p class="st-footnote">Select a worker to manage its copy. Each worker keeps its own copy{kind === 'folder' ? '' : ' and clones with its own GitHub sign-in'}; removing a project from a worker leaves its files on disk.</p>
				</div>
				{#if selected}
					<aside class="pw-inspector" aria-label="{selected.device_name} details">
						<header class="pw-inspector-head">
							<MonitorSmartphone aria-hidden="true" />
							<div class="pw-inspector-title">
								<h3>{selected.device_name}{#if selected.is_self}<span class="pw-tag">This worker</span>{/if}</h3>
								<p class="project-muted">{selected.online ? 'Online' : ['Offline', seenAgo(selected.last_seen)].filter(Boolean).join(' · ')}</p>
							</div>
							<button type="button" class="st-btn icon" aria-label="Close {selected.device_name} details" title="Close" onclick={() => select(null)}><X /></button>
						</header>
						{@render inspector(selected)}
					</aside>
				{/if}
			</div>
		{/if}
	{/if}
</div>
