<script lang="ts">
	/**
	 * ProjectWorkersTab — the project's workers (GET /projects/workers): one
	 * row per approved worker with its status, folder, branch and whether it is
	 * online. Status carries the row's own action: Retry for a failed setup,
	 * Set up (or Set up when online) where the project is missing.
	 *
	 * A row opens that worker's details under it (one worker at a time, the
	 * same on a phone), in these states:
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
	 * Above the rows, a banner names the online workers that lack the project
	 * with "Set up on all missing". While any setup is queued or running the
	 * list is re-read every 2 s; the `projects` resources_changed signal also
	 * refreshes it. Owner/admin only actions; API users never see folders.
	 */
	import { onDestroy, tick, untrack } from 'svelte';
	import { ChevronDown, MessageSquarePlus, RotateCw } from 'lucide-svelte';
	import { RowGroup, SettingRow, EmptyDetail, InlineBanner, PathText } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import { workerDisplayName } from '$lib/components/ui/worker-name';
	import './project-tabs.css';
	import { removalCheckError, removalConfirmText } from './project-removal';
	import ProjectLocalRemoval from './ProjectLocalRemoval.svelte';
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
		seenAgo,
		workerStatus
	} from './project-workers-view';

	interface Props {
		projectRef: ProjectRef;
		projectName: string;
		/** This worker's legacy id for the project, when it is here. */
		localProjectId: string | null;
		/** The worker whose details are open (its row opens them; the parent can too). */
		selectedInstallId?: string | null;
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
		/** The Settings tab sent the owner here: open this worker's removal confirmation in its details. */
		askSelfRemoval?: boolean;
		/** The request above was taken (so a later remount does not ask again). */
		onSelfRemovalAsked?: () => void;
	}

	let { projectRef, projectName, localProjectId, selectedInstallId = $bindable(null), onChanged, onLoaded, onUnavailable, expectLocalId = null, askSelfRemoval = false, onSelfRemovalAsked }: Props = $props();

	let data = $state<ProjectWorkersResponse | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	/**
	 * The last action's failure. One started from a worker's details names that
	 * worker and shows there; one started from the page (Set up on all missing)
	 * has no worker and shows on the page.
	 */
	let actionError = $state<{ installId: string | null; message: string } | null>(null);
	/**
	 * The Settings tab sent the owner to this worker's removal: its confirmation
	 * opens at once. A new id per request, so asking again after Cancel asks again.
	 */
	let askLocal = $state(0);
	let askLocalSeq = 0;
	/**
	 * This worker's removal confirmation is open; cleared with every other
	 * removal state. Its running request and failure are held here too, above
	 * the details that mount with their row: closing and reopening them
	 * mid-DELETE keeps Remove disabled, and the failure lands in the row on
	 * screen. The request alone clears `localRemoving`, so nothing can send a
	 * second one.
	 */
	let localConfirming = $state(false);
	let localRemoving = $state(false);
	let localRemovalError = $state<string | null>(null);
	/** Folder field per worker (seeded from the suggestion, then the owner's edits). */
	let folders = $state<Record<string, string>>({});
	/** Workers whose details are in "use a folder already there" mode. */
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

	// The parent opened a worker (the Settings tab's removal): bring its details
	// into view and focus them, once per selection (not on every poll, which
	// would pull focus out of a folder field). A row the owner pressed keeps
	// the focus itself (see select).
	let focusedFor: string | null = null;
	const loaded = $derived(!!data);
	$effect(() => {
		const id = selectedInstallId;
		if (!id || !loaded) { if (!id) focusedFor = null; return; }
		if (focusedFor === id) return;
		focusedFor = id;
		void tick().then(() => {
			const el = document.getElementById(`pw-card-${id}`);
			const top = el?.getBoundingClientRect().top ?? 0;
			if (top < 0 || top > window.innerHeight - 80) el?.scrollIntoView?.({ block: 'start', behavior: 'smooth' });
			el?.focus({ preventScroll: true });
		});
	});

	// Settings' "Remove from <this worker>": once the list is here, open this
	// worker's details with its removal confirmation already asking.
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
		editing = null;
		askLocal = 0;
		cancelRemove();
		// The row that was pressed keeps the keyboard; its details open under it.
		focusedFor = id;
		selectedInstallId = id;
	}

	/** A row opens its worker's details, and closes them when they are open. */
	function toggle(id: string) {
		select(id === selectedInstallId ? null : id);
	}

	function errMessage(e: unknown, fallback: string): string {
		const err = e as { detail?: unknown; message?: string };
		return typeof err.detail === 'string' ? err.detail : err.message || fallback;
	}

	/** Record a failed action: in its worker's details, or on the page when the page started it. */
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

	function newChatHere(w: ProjectWorkerDetail) {
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
		if (w.is_self) layout.openFeaturePage('settings', 'category=integrations&item=github');
		else githubHelp = githubHelp === w.install_id ? null : w.install_id;
	}

	/** A row's own action (Retry, Set up): it opens the details too, so the result shows there. */
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

	/** Online, or offline and when it was last seen (the row's first line). */
	function presence(w: ProjectWorkerDetail): string {
		return w.online ? 'Online' : ['Offline', seenAgo(w.last_seen)].filter(Boolean).join(' · ');
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

<!-- A worker's details, under its row: flat rows of the Workers card (no card of their own). -->
{#snippet details(w: ProjectWorkerDetail)}
	{@const running = isRunning(w)}
	{@const has = hasProject(w.state)}
	{@const failed = w.state === 'failed'}
	{@const action = failed ? errorAction(w.setup?.error_code, w.device_name) : null}
	{@const check = confirmingRemove === w.install_id && removalCheck?.installId === w.install_id ? removalCheck : null}
	{@const fresh = check?.detail ?? null}
	<section id="pw-card-{w.install_id}" class="pw-card" tabindex="-1" aria-label="{w.device_name} details" data-worker-state={w.state}>
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
			<div class="pw-card-banner"><InlineBanner>{actionError.message}{#snippet actions()}<button class="st-btn" onclick={() => (actionError = null)}>Dismiss</button>{/snippet}</InlineBanner></div>
		{/if}
		<SettingRow label={running ? (kind === 'folder' ? 'Setting up…' : 'Cloning…') : has ? 'Ready' : w.state === 'queued_offline' ? `Waiting for ${w.device_name}` : failed ? (kind === 'folder' ? "Couldn't set it up" : "Couldn't clone") : w.state === 'unknown' ? `Couldn't check ${w.device_name}` : `Not set up ${w.is_self ? 'on this worker' : 'here'}`}
			hint={has ? (w.online ? `Start a chat in ${projectName} on ${w.is_self ? 'this worker' : w.device_name}.` : `${w.device_name} is offline.`) : undefined} wrapHint expanded={running || w.state === 'queued_offline' || w.state === 'unknown' || failed ? statusDetail : undefined}>
			{#if running && $isAdmin && w.setup?.op_id}<button class="st-btn" onclick={() => cancel(w)} disabled={busy[w.install_id]}>Cancel setup</button>
			{:else if has}
				{#if w.online}<button class="st-btn" onclick={() => newChatHere(w)} disabled={busy[w.install_id]}><MessageSquarePlus />New chat here</button>{/if}
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
		{#if has || running}
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
		{/if}
		{#if has && $isAdmin && editing?.installId !== w.install_id && (w.is_self ? !!localProjectId : w.online)}
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
		{/if}
	</section>
{/snippet}

<div data-ui-project-workers={loading ? 'loading' : loadError ? 'error' : 'ready'}>
	{#if loading && !data}<p class="project-muted" role="status" aria-busy="true" aria-label="Loading workers">Loading workers…</p>
	{:else if loadError && !data}
		<InlineBanner>{loadError}{#snippet actions()}<button class="st-btn" onclick={() => { loading = true; void load(); }}>Try again</button>{/snippet}</InlineBanner>
	{:else if data}
		<!-- A worker's own failure shows in its open details. -->
		{#if actionError && !(actionError.installId && actionError.installId === selected?.install_id)}<InlineBanner>{actionError.message}{#snippet actions()}<button class="st-btn" onclick={() => (actionError = null)}>Dismiss</button>{/snippet}</InlineBanner>{/if}
		{#if $isAdmin && orphaned}
			<InlineBanner tone="info">No worker has this project any more. You can take it off the Projects list from the Settings tab; its chats keep it and nothing on disk is touched.</InlineBanner>
		{/if}
		{#if $isAdmin && missingOnline.length > 0}
			{@const names = nameList(missingOnline.map(w => w.device_name))}
			{@const onlySelf = missingOnline.length === 1 && missingOnline[0].is_self}
			<InlineBanner tone="info">{#if onlySelf}{projectName} isn't on {names} yet. Set it up here to start chats here and edit its MCP servers, agents, hooks and memory.{:else}{names} {missingOnline.length === 1 ? "doesn't" : "don't"} have this project yet. Set it up there to start chats on {names} too.{/if}
				{#snippet actions()}<button class="st-btn" onclick={setUpAllMissing} disabled={missingOnline.some(w => busy[w.install_id])}>{onlySelf ? 'Set up here' : 'Set up on all missing'}</button>{/snippet}
			</InlineBanner>
		{/if}
		{#if !workers.length}<EmptyDetail title="No other workers" message="Pair a worker in Settings to run this project there too." />
		{:else}
			<RowGroup title="Workers" count={workers.length}>
				<div class="pw-rows">
					{#each workers as w (w.install_id)}
						{@const folder = folderOf(w)}
						{@const branch = kind === 'git' ? branchLine(w) : null}
						{@const open = w.install_id === selectedInstallId}
						<div class="pw-item" class:open data-worker-state={w.state}>
							<!-- The row is a large click target; its button is the keyboard path. -->
							<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
							<div class="pw-row" onclick={() => toggle(w.install_id)}>
								<button id="pw-row-{w.install_id}" type="button" class="pw-row-btn" aria-expanded={open} aria-controls={open ? `pw-card-${w.install_id}` : undefined} onclick={(e) => { e.stopPropagation(); toggle(w.install_id); }}>
									<span class="pw-row-line"><span class="pw-row-name" title={w.device_name}>{w.device_name}</span>{#if w.is_self}<span class="pw-tag">This worker</span>{/if}<span class="pw-row-seen">{presence(w)}</span></span>
									{#if folder || branch}
										<span class="pw-row-sub">{#if folder}<span class="pw-row-folder"><PathText path={folder} /></span>{/if}{#if branch}<span class="pw-row-branch">{branch}</span>{/if}</span>
									{/if}
								</button>
								{@render statusCell(w)}
								<span class="pw-row-action">{@render rowAction(w)}</span>
								<ChevronDown class="pw-chev" aria-hidden="true" />
							</div>
							{#if open}{@render details(w)}{/if}
						</div>
					{/each}
				</div>
			</RowGroup>
			<p class="st-footnote">Open a worker to manage its copy. Each worker keeps its own copy{kind === 'folder' ? '' : ' and clones with its own GitHub sign-in'}; removing a project from a worker leaves its files on disk.</p>
		{/if}
	{/if}
</div>
