<script lang="ts">
	/**
	 * ProjectWorkersTab — one card per approved worker for a project
	 * (GET /projects/workers), in the states of the approved mockup:
	 *
	 *   ready           folder, branch · clean / N changed files, New chat here, Remove
	 *   missing         editable folder (suggested), what a setup does, Set up, Use a folder already there…
	 *   setting up      progress (indeterminate when unknown), "You can leave this page", Cancel
	 *   failed          the plain reason, the fix for its code, Retry (folder stays editable)
	 *   offline         "Set up when online" (queues it) / "Waiting for <worker>" with Withdraw
	 *
	 * A Git project gets a Repository section on top: "Change repository" points
	 * the project at another repository (a new one or a fork) on every worker at
	 * once (PUT /projects/catalog/repository); it stays one project. When no
	 * worker holds a copy any more, a banner offers to take it off the list
	 * (POST /projects/catalog/forget); nothing is deleted.
	 *
	 * Above the cards, a banner names the online workers that lack the project
	 * with "Set up on all missing". While any setup is queued or running the
	 * list is re-read every 2 s; the `projects` resources_changed signal also
	 * refreshes it. Owner/admin only actions; API users never see folders.
	 */
	import { onDestroy, tick, untrack } from 'svelte';
	import { Check, X, Loader2, FolderOpen, MessageSquarePlus, RotateCw, Github } from 'lucide-svelte';
	import { RowGroup, SettingRow, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import Switch from '$lib/components/settings/rows/Switch.svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import { workerDisplayName } from '$lib/components/ui/worker-name';
	import './project-tabs.css';
	import { removalCheckError, removalConfirmText } from './project-removal';
	import {
		browseWorkerFolder,
		cancelProjectSetup,
		changeProjectRepository,
		changeWorkerFolder,
		forgetProject,
		getProjectWorkers,
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
		repositoryChangeSummary,
		seenAgo,
		setupVerb
	} from './project-workers-view';

	interface Props {
		projectRef: ProjectRef;
		projectName: string;
		/** This worker's legacy id for the project, when it is here. */
		localProjectId: string | null;
		/** Focus this worker's card once loaded (a chip was clicked). */
		focusInstallId?: string | null;
		/** The project's workers changed (a setup started, finished or was removed). */
		onChanged?: () => void;
		/** The project was taken off the list (no worker held it). */
		onRemoved?: () => void;
	}

	let { projectRef, projectName, localProjectId, focusInstallId = null, onChanged, onRemoved }: Props = $props();

	let data = $state<ProjectWorkersResponse | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let actionError = $state<string | null>(null);
	/** Folder field per worker (seeded from the suggestion, then the owner's edits). */
	let folders = $state<Record<string, string>>({});
	/** Workers whose card is in "use a folder already there" mode. */
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
	/** The "Change repository" form while it is open. */
	let repoDraft = $state<{ url: string; keepUpstream: boolean; error: string | null } | null>(null);
	let repoBusy = $state(false);
	/** What the last repository change did, worker by worker. */
	let repoResult = $state<{ ok: boolean; lines: string[] } | null>(null);
	let forgetBusy = $state(false);
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
			// A setup on this worker just finished: reload its catalog so the
			// project's local tabs unlock without waiting for the live-sync push.
			const selfWas = data?.workers.find((w) => w.is_self)?.state;
			const selfNow = res.workers.find((w) => w.is_self)?.state;
			const selfArrived = !!data && selfWas !== 'ready' && selfNow === 'ready';
			data = res;
			loadError = null;
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
			repoDraft = null;
			repoResult = null;
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

	// A chip click: bring that worker's card into view and focus it, once per
	// click (not on every poll, which would pull focus out of a folder field).
	let focusedFor: string | null = null;
	const loaded = $derived(!!data);
	$effect(() => {
		const id = focusInstallId;
		if (!id || !loaded) { if (!id) focusedFor = null; return; }
		if (focusedFor === id) return;
		focusedFor = id;
		void tick().then(() => {
			const el = document.getElementById(`pw-card-${id}`);
			el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
			el?.focus({ preventScroll: true });
		});
	});

	function errMessage(e: unknown, fallback: string): string {
		const err = e as { detail?: unknown; message?: string };
		return typeof err.detail === 'string' ? err.detail : err.message || fallback;
	}

	async function setUp(w: ProjectWorkerDetail, mode: 'auto' | 'adopt' = 'auto') {
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
			actionError = `Couldn't start the setup on ${w.device_name}: ${errMessage(e, 'unknown error')}`;
		} finally {
			busy = { ...busy, [w.install_id]: false };
		}
	}

	async function setUpAllMissing() {
		await Promise.all(missingOnline.map((w) => setUp(w)));
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
			if ((e as { status?: number }).status !== 409) actionError = errMessage(e, "Couldn't cancel it.");
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
			actionError = `Couldn't remove it from ${w.device_name}: ${errMessage(e, 'unknown error')}`;
		} finally {
			busy = { ...busy, [w.install_id]: false };
		}
	}

	function openFolderEditor(w: ProjectWorkerDetail) {
		if (!w.folder_revision || !w.folder || !w.online || loadError) return;
		cancelRemove();
		editing = { installId: w.install_id, revision: w.folder_revision, folder: w.folder, error: null, browsing: false };
		void tick().then(() => {
			const input = document.querySelector<HTMLInputElement>(`#${CSS.escape(`pw-card-${w.install_id}`)} [data-change-folder-input] input`);
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

	function openRepoEditor() {
		repoResult = null;
		repoDraft = { url: '', keepUpstream: false, error: null };
		void tick().then(() => document.querySelector<HTMLInputElement>('[data-change-repo-input] input')?.focus());
	}
	
	function closeRepoEditor() {
		repoDraft = null;
		void tick().then(() => document.getElementById('pw-change-repo')?.focus());
	}
	
	async function saveRepository() {
		const draft = repoDraft;
		const url = draft?.url.trim();
		if (!draft || !url || repoBusy) return;
		repoBusy = true;
		draft.error = null;
		const projectKey = refKey;
		try {
			const res = await changeProjectRepository(projectRef, url, draft.keepUpstream);
			if (destroyed || projectKey !== refKey) return;
			repoResult = repositoryChangeSummary(res);
			if (repoDraft === draft) repoDraft = null;
			if (res.workers.some((w) => w.is_self)) void appData.loadProjectCatalog(performance.now());
			onChanged?.();
			void load();
		} catch (e) {
			if (destroyed || projectKey !== refKey || repoDraft !== draft) return;
			draft.error = errMessage(e, "Couldn't change the repository.");
		} finally {
			if (!destroyed && projectKey === refKey) repoBusy = false;
		}
	}
	
	async function forget() {
		forgetBusy = true;
		actionError = null;
		try {
			await forgetProject(projectRef);
			onChanged?.();
			onRemoved?.();
		} catch (e) {
			actionError = `Couldn't remove it from the list: ${errMessage(e, 'unknown error')}`;
			void load();
		} finally {
			forgetBusy = false;
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

	const placeName = (w: ProjectWorkerDetail) => (w.is_self ? 'here' : `on ${w.device_name}`);
</script>
<div data-ui-project-workers={loading ? 'loading' : loadError ? 'error' : 'ready'}>
	{#if loading && !data}<p class="project-muted" role="status" aria-busy="true" aria-label="Loading workers">Loading workers…</p>
	{:else if loadError && !data}
		<InlineBanner>{loadError}{#snippet actions()}<button class="st-btn" onclick={() => { loading = true; void load(); }}>Try again</button>{/snippet}</InlineBanner>
	{:else if data}
		{#if actionError}<InlineBanner>{actionError}{#snippet actions()}<button class="st-btn" onclick={() => (actionError = null)}>Dismiss</button>{/snippet}</InlineBanner>{/if}
		{#if repoResult}
			<InlineBanner tone={repoResult.ok ? 'info' : undefined}><span data-repo-result={repoResult.ok ? 'ok' : 'partial'}>{repoResult.lines.join(' ')}</span>
				{#snippet actions()}<button class="st-btn" onclick={() => (repoResult = null)}>Dismiss</button>{/snippet}
			</InlineBanner>
		{/if}
		{#if $isAdmin && orphaned}
			<InlineBanner tone="info">No worker has this project any more. Remove it from the Projects list? Its chats keep it, nothing on disk is touched, and it comes back if you set it up again.
				{#snippet actions()}<button class="st-btn" onclick={forget} disabled={forgetBusy}>{forgetBusy ? 'Removing…' : 'Remove from list'}</button>{/snippet}
			</InlineBanner>
		{/if}
		{#if kind === 'git'}
			<RowGroup title="Repository">
				<SettingRow label="Repository" hint={data.remote || 'Not reported'} hintMono>
					{#if $isAdmin && !repoDraft}<button id="pw-change-repo" class="st-btn" onclick={openRepoEditor} disabled={!!loadError}><Github />Change repository</button>{/if}
				</SettingRow>
				{#if repoDraft && $isAdmin}
					{@const draft = repoDraft}
					<SettingRow label="New repository" wrapHint hint="A new repository or your fork. Every worker's copy switches to it and fetches it; branches, files and uncommitted work stay as they are. Workers that are offline switch when they come back. It stays one project.">
						{#snippet expanded()}
							<form id="pw-repo-form" class="flex flex-col gap-2" onsubmit={(event) => { event.preventDefault(); void saveRepository(); }}>
								<div data-change-repo-input><TextField bind:value={draft.url} width="full" mono label="New repository URL" placeholder="https://github.com/you/project.git" disabled={repoBusy} /></div>
								{#if draft.error}<p class="project-error" role="alert">{draft.error}</p>{/if}
							</form>
						{/snippet}
					</SettingRow>
					{#if data.remote}
						<SettingRow label="Keep the current one as upstream" hint={`For a fork: ${data.remote} stays reachable as the upstream remote, so you can still pull from it.`}>
							<Switch bind:checked={draft.keepUpstream} disabled={repoBusy} />
						</SettingRow>
					{/if}
					<SettingRow label="Actions">
						<button type="button" class="st-btn" disabled={repoBusy} onclick={closeRepoEditor}>Cancel</button>
						<button type="submit" form="pw-repo-form" class="st-btn primary" disabled={repoBusy || !draft.url.trim()}>{repoBusy ? 'Switching…' : 'Switch repository'}</button>
					</SettingRow>
				{/if}
			</RowGroup>
		{/if}
		{#if $isAdmin && missingOnline.length > 0}
			{@const names = nameList(missingOnline.map(w => w.device_name))}
			<InlineBanner tone="info">{names} {missingOnline.length === 1 ? "doesn't" : "don't"} have this project yet. Set it up there to start chats on {names} too.
				{#snippet actions()}<button class="st-btn" onclick={setUpAllMissing} disabled={missingOnline.some(w => busy[w.install_id])}>Set up on all missing</button>{/snippet}
			</InlineBanner>
		{/if}
		{#if !workers.length}<EmptyDetail title="No other workers" message="Pair a worker in Settings to run this project there too." />
		{:else}
			{#each workers as w (w.install_id)}
				{@const running = isRunning(w)}
				{@const has = hasProject(w.state)}
				{@const failed = w.state === 'failed'}
				{@const action = failed ? errorAction(w.setup?.error_code, w.device_name) : null}
				{@const check = confirmingRemove === w.install_id && removalCheck?.installId === w.install_id ? removalCheck : null}
				{@const fresh = check?.detail ?? null}
				<article id="pw-card-{w.install_id}" tabindex="-1" aria-label={w.device_name} data-worker-state={w.state}>
					{#snippet statusDetail()}
						{#if w.state === 'queued_offline'}<p class="project-muted">It sets up when {w.device_name} comes back online.</p>
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
					<RowGroup title={w.device_name}>
						<SettingRow label="Status" hint={[w.is_self ? 'This worker' : '', w.online ? 'Online' : ['Offline', seenAgo(w.last_seen)].filter(Boolean).join(' · ')].filter(Boolean).join(' · ')} expanded={w.state === 'queued_offline' || w.state === 'unknown' || failed ? statusDetail : undefined}>
							<span class:project-error={failed} class="project-muted">{running ? (kind === 'folder' ? 'Setting up…' : 'Cloning…') : has ? 'Ready' : w.state === 'queued_offline' ? `Waiting for ${w.device_name}` : failed ? (kind === 'folder' ? "Couldn't set it up" : "Couldn't clone") : w.state === 'unknown' ? `Couldn't check ${w.device_name}` : `Not set up ${w.is_self ? 'on this worker' : 'here'}`}</span>
							{#if running}
								{@const p = w.setup?.progress}
								<progress class="project-progress" max="100" value={typeof p === 'number' ? p : undefined} aria-label="Setting up on {w.device_name}"></progress>
							{/if}
						</SettingRow>
						{#if has || running}
							<SettingRow label="Folder" hint={w.folder || folders[w.install_id] || w.suggested_folder || 'Not reported'} hintMono />
							{#if kind === 'git'}<SettingRow label="Branch" hint={branchLine(w) || (running ? 'Pending setup' : 'Not reported')} />{/if}
						{:else if w.state !== 'queued_offline' && w.state !== 'unknown'}
							{#if $isAdmin && w.online}
								<SettingRow label={adopting[w.install_id] ? `Folder already on ${w.device_name}` : `Folder on ${w.device_name}`} hint={setupHint(w)}>
									{#snippet expanded()}
										<form id="pw-folder-{w.install_id}" onsubmit={(e) => { e.preventDefault(); setUp(w, adopting[w.install_id] ? 'adopt' : 'auto'); }}>
											<TextField onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); setUp(w, adopting[w.install_id] ? 'adopt' : 'auto'); } }} value={folders[w.install_id] ?? ''} oninput={(v) => (folders = { ...folders, [w.install_id]: v })} mono width={600} placeholder={w.suggested_folder ?? data?.default_folder} label={adopting[w.install_id] ? `Folder already on ${w.device_name}` : `Folder on ${w.device_name}`} disabled={busy[w.install_id]} />
										</form>
									{/snippet}
								</SettingRow>
							{:else if !w.online}<SettingRow label="Worker offline" hint={`Turn ${w.device_name} on to set it up — or queue it and it runs when ${w.device_name} comes back.`} />{/if}
						{/if}
						{#if githubHelp === w.install_id}<SettingRow label="GitHub sign-in" hint={`On ${w.device_name}, open AI Shuffle, go to Settings → Integrations → GitHub and sign in. Then press Retry here.`} />{/if}
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
						<SettingRow label="Actions" wrapHint={confirmingRemove === w.install_id}
							hint={confirmingRemove !== w.install_id ? undefined
								: fresh ? removalConfirmText(projectName, w.device_name, { chats: fresh.removal_chats ?? null, worktrees: fresh.removal_worktree_count ?? null })
								: check?.error ? `Remove ${projectName} from ${w.device_name}?` : 'Counting the chats this removes…'}
							expanded={check?.error || fresh?.removal_worktrees?.length ? removalDetail : undefined}>
							{#if running && $isAdmin && w.setup?.op_id}<button class="st-btn" onclick={() => cancel(w)} disabled={busy[w.install_id]}>Cancel</button>
							{:else if has}
								{#if w.online}<button class="st-btn" onclick={() => newChatHere(w)} disabled={busy[w.install_id]}><MessageSquarePlus />New chat here</button>{/if}
								{#if $isAdmin && editing?.installId !== w.install_id && (w.folder_revision || !w.online)}
									<button id={`pw-change-${w.install_id}`} class="st-btn" disabled={!!editing || !w.online || !w.folder_revision || busy[w.install_id] || !!loadError} onclick={() => openFolderEditor(w)}>Change folder</button>
								{/if}
								{#if $isAdmin && !w.is_self && w.online && editing?.installId !== w.install_id}
									{#if confirmingRemove === w.install_id}<button class="st-btn" onclick={cancelRemove} disabled={busy[w.install_id]}>Cancel</button><button class="st-btn danger" onclick={() => remove(w)} disabled={busy[w.install_id] || !fresh}>{busy[w.install_id] ? 'Removing…' : 'Remove'}</button>
									{:else}<button class="st-btn danger" onclick={() => askRemove(w)}>Remove project and chats from {w.device_name}</button>{/if}
								{/if}
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
					</RowGroup>
				</article>
			{/each}
			<p class="st-footnote">Each worker keeps its own copy and clones with its own GitHub sign-in. Removing a project from a worker leaves its files on disk. You can leave this page; setup keeps going on the worker.</p>
		{/if}
	{/if}
</div>
