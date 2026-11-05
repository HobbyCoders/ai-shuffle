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
	 * Above the cards, a banner names the online workers that lack the project
	 * with "Set up on all missing". While any setup is queued or running the
	 * list is re-read every 2 s; the `projects` resources_changed signal also
	 * refreshes it. Owner/admin only actions; API users never see folders.
	 */
	import { onDestroy, tick, untrack } from 'svelte';
	import { Check, X, Loader2, FolderOpen, MessageSquarePlus, Trash2, RotateCw, Github } from 'lucide-svelte';
	import { RowGroup, SettingRow, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import './project-tabs.css';
	import {
		cancelProjectSetup,
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
		isRunning,
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
	}

	let { projectRef, projectName, localProjectId, focusInstallId = null, onChanged }: Props = $props();

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
	let confirmingRemove = $state<string | null>(null);
	let githubHelp = $state<string | null>(null);
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let seq = 0;
	let destroyed = false;
	let lastSignature = '';

	const refKey = $derived(JSON.stringify(projectRef));
	const kind = $derived(data?.kind ?? null);
	const workers = $derived(data?.workers ?? []);
	const missingOnline = $derived(workers.filter((w) => w.online && (w.state === 'missing') && !isRunning(w)));
	const anyRunning = $derived(workers.some((w) => isRunning(w)));

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
		const err = e as { detail?: string; message?: string };
		return err.detail || err.message || fallback;
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

	async function remove(w: ProjectWorkerDetail) {
		busy = { ...busy, [w.install_id]: true };
		actionError = null;
		try {
			await removeProjectFromWorker(w.install_id, projectRef);
			confirmingRemove = null;
			if (w.is_self) await appData.loadProjectCatalog(performance.now());
			onChanged?.();
			void load();
		} catch (e) {
			actionError = `Couldn't remove it from ${w.device_name}: ${errMessage(e, 'unknown error')}`;
		} finally {
			busy = { ...busy, [w.install_id]: false };
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
		if (kind === 'folder') return `Creates an empty ${data?.default_folder || projectName} folder; files aren't copied.`;
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
				<article id="pw-card-{w.install_id}" tabindex="-1" aria-label={w.device_name} data-worker-state={w.state}>
					<RowGroup title={w.device_name}>
						<SettingRow label="Status" hint={[w.is_self ? 'This worker' : '', w.online ? 'Online' : ['Offline', seenAgo(w.last_seen)].filter(Boolean).join(' · ')].filter(Boolean).join(' · ')}>
							<span class:project-error={failed} class="project-muted">{running ? (kind === 'folder' ? 'Setting up…' : 'Cloning…') : has ? 'Ready' : w.state === 'queued_offline' ? `Waiting for ${w.device_name}` : failed ? (kind === 'folder' ? "Couldn't set it up" : "Couldn't clone") : w.state === 'unknown' ? `Couldn't check ${w.device_name}` : `Not set up ${w.is_self ? 'on this worker' : 'here'}`}</span>
							{#if running}
								{@const p = w.setup?.progress}
								<progress class="project-progress" max="100" value={typeof p === 'number' ? p : undefined} aria-label="Setting up on {w.device_name}"></progress>
							{/if}
							{#snippet expanded()}
								{#if w.state === 'queued_offline'}<p class="project-muted">It sets up when {w.device_name} comes back online.</p>
								{:else if w.state === 'unknown'}<p class="project-muted">{w.device_name} is online but didn't answer. It may still have this project.</p>
								{:else if failed}<p class="project-error">{failureReason(w)}</p>{/if}
							{/snippet}
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
						<SettingRow label="Actions" hint={confirmingRemove === w.install_id ? `Remove from ${w.device_name}? Its files stay.` : undefined}>
							{#if running && $isAdmin && w.setup?.op_id}<button class="st-btn" onclick={() => cancel(w)} disabled={busy[w.install_id]}>Cancel</button>
							{:else if has}
								{#if w.online}<button class="st-btn" onclick={() => newChatHere(w)}><MessageSquarePlus />New chat here</button>{/if}
								{#if $isAdmin && (w.online || w.is_self)}
									{#if confirmingRemove === w.install_id}<button class="st-btn" onclick={() => (confirmingRemove = null)} disabled={busy[w.install_id]}>Cancel</button><button class="st-btn danger" onclick={() => remove(w)} disabled={busy[w.install_id]}>{busy[w.install_id] ? 'Removing…' : 'Remove'}</button>
									{:else}<button class="st-btn danger" onclick={() => (confirmingRemove = w.install_id)}><Trash2 />Remove from {w.device_name}</button>{/if}
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
			<p class="project-muted">Each worker keeps its own copy and clones with its own GitHub sign-in. Removing a project from a worker leaves its files on disk. You can leave this page; setup keeps going on the worker.</p>
		{/if}
	{/if}
</div>
