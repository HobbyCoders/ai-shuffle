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
	import Banner from '$lib/components/Banner.svelte';
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
		const input = document.getElementById(`pw-folder-${id}`) as HTMLInputElement | null;
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

<div class="pw" data-ui-project-workers={loading ? 'loading' : loadError ? 'error' : 'ready'}>
	{#if loading && !data}
		<div class="pw-grid" aria-busy="true" aria-label="Loading workers">
			{#each [0, 1, 2] as i (i)}<div class="pw-card pw-skel"></div>{/each}
		</div>
	{:else if loadError && !data}
		<Banner severity="error" message={loadError} action={{ label: 'Try again', onClick: () => { loading = true; void load(); } }} />
	{:else if data}
		{#if actionError}
			<Banner severity="error" message={actionError} dismissible onDismiss={() => (actionError = null)} />
		{/if}

		{#if $isAdmin && missingOnline.length > 0}
			{@const names = nameList(missingOnline.map((w) => w.device_name))}
			<div class="pw-callout" role="status">
				<p><b>{names} {missingOnline.length === 1 ? "doesn't" : "don't"} have this project yet.</b> Set it up there and you can start chats on {names} too.</p>
				<button type="button" class="pc-btn pc-btn--primary pc-btn--sm" onclick={setUpAllMissing} disabled={missingOnline.some((w) => busy[w.install_id])}>
					Set up on all missing
				</button>
			</div>
		{/if}

		{#if workers.length === 0}
			<div class="pw-empty">
				<FolderOpen size={30} strokeWidth={1.25} />
				<p>No other workers in this network yet. Pair one in Settings → Workers to run this project there too.</p>
			</div>
		{:else}
			<div class="pw-grid">
				{#each workers as w (w.install_id)}
					{@const running = isRunning(w)}
					{@const has = hasProject(w.state)}
					{@const failed = w.state === 'failed'}
					{@const missing = !running && !has && !failed && (w.state === 'missing' || w.state === 'offline_missing')}
					{@const action = failed ? errorAction(w.setup?.error_code, w.device_name) : null}
					<article
						id="pw-card-{w.install_id}"
						class="pw-card"
						class:pw-card--missing={missing || w.state === 'queued_offline'}
						class:pw-card--failed={failed}
						tabindex="-1"
						aria-labelledby="pw-name-{w.install_id}"
						data-worker-state={w.state}
					>
						<header class="pw-top">
							<b id="pw-name-{w.install_id}">{w.device_name}</b>
							{#if w.is_self}<span class="pw-tag">this worker</span>{/if}
							<span class="sp"></span>
							<span class="pw-pres" class:off={!w.online}>
								<i aria-hidden="true"></i>{w.online ? 'online' : ['offline', seenAgo(w.last_seen)].filter(Boolean).join(' · ')}
							</span>
						</header>

						{#if running}
							{@const p = w.setup?.progress}
							<div class="pw-state pw-state--busy">
								<Loader2 size={14} class="pw-spin" aria-hidden="true" />
								{kind === 'folder' ? 'Setting up…' : 'Cloning…'}{#if typeof p === 'number'} {Math.round(p)}%{/if}
							</div>
							<div
								class="pw-bar"
								class:indeterminate={typeof p !== 'number'}
								role="progressbar"
								aria-label="Setting up on {w.device_name}"
								aria-valuemin={0}
								aria-valuemax={100}
								aria-valuenow={typeof p === 'number' ? Math.round(p) : undefined}
							><i style:width={typeof p === 'number' ? `${Math.max(3, Math.min(100, p))}%` : undefined}></i></div>
							<p class="pw-hint">You can leave this page; it keeps going on {w.device_name}.</p>
							{#if $isAdmin && w.setup?.op_id}
								<div class="pw-acts">
									<button type="button" class="pc-btn pc-btn--sm" onclick={() => cancel(w)} disabled={busy[w.install_id]}>Cancel</button>
								</div>
							{/if}
						{:else if has}
							<div class="pw-state pw-state--ok"><Check size={14} strokeWidth={2.4} aria-hidden="true" />Ready</div>
							{#if w.folder || branchLine(w)}
								<dl class="pw-kv">
									{#if w.folder}<dt>Folder</dt><dd><code>{w.folder}</code></dd>{/if}
									{#if branchLine(w)}<dt>Branch</dt><dd><code>{branchLine(w)}</code></dd>{/if}
								</dl>
							{/if}
							<div class="pw-acts">
								{#if w.online}
									<button type="button" class="pc-btn pc-btn--sm pc-btn--ghost" onclick={() => newChatHere(w)}>
										<MessageSquarePlus size={13} strokeWidth={1.8} aria-hidden="true" />New chat here
									</button>
								{/if}
								{#if $isAdmin && (w.online || w.is_self)}
									{#if confirmingRemove === w.install_id}
										<span class="pw-confirm">Remove from {w.device_name}? Its files stay.</span>
										<button type="button" class="pc-btn pc-btn--sm" onclick={() => (confirmingRemove = null)} disabled={busy[w.install_id]}>Cancel</button>
										<button type="button" class="pc-btn pc-btn--sm pc-btn--danger" onclick={() => remove(w)} disabled={busy[w.install_id]}>
											{#if busy[w.install_id]}<Loader2 size={13} class="pw-spin" />{/if}Remove
										</button>
									{:else}
										<button type="button" class="pc-btn pc-btn--sm pc-btn--ghost pc-btn--danger-text" onclick={() => (confirmingRemove = w.install_id)}>
											<Trash2 size={13} strokeWidth={1.8} aria-hidden="true" />Remove from {w.device_name}
										</button>
									{/if}
								{/if}
							</div>
						{:else if w.state === 'queued_offline'}
							<div class="pw-state pw-state--mut"><Loader2 size={14} class="pw-spin pw-spin--slow" aria-hidden="true" />Waiting for {w.device_name}</div>
							<p class="pw-hint">It sets up when {w.device_name} comes back online.</p>
							{#if $isAdmin && w.setup?.op_id}
								<div class="pw-acts">
									<button type="button" class="pc-btn pc-btn--sm pc-btn--ghost" onclick={() => cancel(w)} disabled={busy[w.install_id]}>Withdraw</button>
								</div>
							{/if}
						{:else if w.state === 'unknown'}
							<div class="pw-state pw-state--mut">Couldn't check {w.device_name}</div>
							<p class="pw-hint">{w.device_name} is online but didn't answer. It may still have this project.</p>
							<div class="pw-acts">
								<button type="button" class="pc-btn pc-btn--sm pc-btn--ghost" onclick={() => load()}><RotateCw size={13} aria-hidden="true" />Check again</button>
							</div>
						{:else}
							{#if failed}
								<div class="pw-state pw-state--err"><X size={14} strokeWidth={2.4} aria-hidden="true" />{kind === 'folder' ? "Couldn't set it up" : "Couldn't clone"}</div>
								<p class="pw-hint pw-hint--fg">{failureReason(w)}</p>
							{:else}
								<div class="pw-state pw-state--mut">Not set up {w.is_self ? 'on this worker' : 'here'}</div>
							{/if}
							{#if $isAdmin && w.online}
								<label class="pw-field" for="pw-folder-{w.install_id}">
									<span>{adopting[w.install_id] ? `Folder already on ${w.device_name}` : `Folder on ${w.device_name}`}</span>
									<input
										id="pw-folder-{w.install_id}"
										type="text"
										class="pw-input"
										bind:value={folders[w.install_id]}
										placeholder={w.suggested_folder ?? data.default_folder}
										spellcheck="false"
										autocomplete="off"
										onkeydown={(e) => { if (e.key === 'Enter') setUp(w, adopting[w.install_id] ? 'adopt' : 'auto'); }}
									/>
									<span class="pw-hint">{setupHint(w)}</span>
								</label>
							{:else if !w.online}
								<p class="pw-hint">Turn {w.device_name} on to set it up — or queue it and it runs when {w.device_name} comes back.</p>
							{/if}
							{#if githubHelp === w.install_id}
								<p class="pw-help" role="note">
									<Github size={13} aria-hidden="true" />
									On {w.device_name}, open AI Shuffle, go to Settings → Integrations → GitHub and sign in. Then press Retry here.
								</p>
							{/if}
							{#if $isAdmin}
								<div class="pw-acts">
									{#if !w.online}
										<button type="button" class="pc-btn pc-btn--sm" onclick={() => setUp(w)} disabled={busy[w.install_id]}>
											{#if busy[w.install_id]}<Loader2 size={13} class="pw-spin" />{/if}Set up when online
										</button>
									{:else if failed}
										{#if action}
											<button type="button" class="pc-btn pc-btn--sm" onclick={() => onErrorAction(w)}>{action.label}</button>
										{/if}
										<button type="button" class="pc-btn pc-btn--sm pc-btn--ghost" onclick={() => setUp(w)} disabled={busy[w.install_id]}>
											{#if busy[w.install_id]}<Loader2 size={13} class="pw-spin" />{:else}<RotateCw size={13} aria-hidden="true" />{/if}Retry
										</button>
									{:else if adopting[w.install_id]}
										<button type="button" class="pc-btn pc-btn--primary pc-btn--sm" onclick={() => setUp(w, 'adopt')} disabled={busy[w.install_id] || !(folders[w.install_id] ?? '').trim()}>
											{#if busy[w.install_id]}<Loader2 size={13} class="pw-spin" />{/if}Use this folder
										</button>
										<button type="button" class="pc-btn pc-btn--sm pc-btn--ghost" onclick={() => (adopting = { ...adopting, [w.install_id]: false })}>Back</button>
									{:else}
										<button type="button" class="pc-btn pc-btn--primary pc-btn--sm" onclick={() => setUp(w)} disabled={busy[w.install_id]}>
											{#if busy[w.install_id]}<Loader2 size={13} class="pw-spin" />{/if}Set up {placeName(w)}
										</button>
										<button type="button" class="pc-btn pc-btn--sm pc-btn--ghost" onclick={() => { adopting = { ...adopting, [w.install_id]: true }; void tick().then(() => focusFolder(w.install_id)); }}>
											Use a folder already there…
										</button>
									{/if}
								</div>
							{/if}
						{/if}
					</article>
				{/each}
			</div>
			<p class="pw-foot">Each worker keeps its own copy and clones with its own GitHub sign-in. Removing a project from a worker never deletes its files.</p>
		{/if}
	{/if}
</div>

<style>
	.pw { display: flex; flex-direction: column; gap: 12px; padding-top: 14px; }
	.pw-callout {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		padding: 11px 13px;
		border: 1px solid color-mix(in oklch, var(--primary) 35%, transparent);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		border-radius: calc(var(--radius, 0.75rem) - 1px);
		font-size: 0.8125rem;
		color: var(--foreground);
	}
	.pw-callout p { flex: 1; min-width: 200px; margin: 0; }

	.pw-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; align-items: start; }
	.pw-card {
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
		padding: 13px 14px;
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		background: var(--card);
		transition: border-color 160ms ease, box-shadow 160ms ease;
	}
	.pw-card:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.pw-card--missing { border-style: dashed; background: transparent; }
	.pw-card--failed { border-color: color-mix(in oklab, var(--destructive) 40%, var(--border)); }
	.pw-skel { min-height: 132px; background: linear-gradient(90deg, var(--card), var(--hover-overlay), var(--card)); background-size: 200% 100%; animation: pw-shimmer 1.4s ease-in-out infinite; }

	.pw-top { display: flex; align-items: center; gap: 8px; min-width: 0; }
	.pw-top b { font-size: 0.875rem; font-weight: 650; color: var(--foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.pw-top .sp { flex: 1; }
	.pw-tag { flex: none; font-size: 0.6875rem; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted-foreground); }
	.pw-pres { flex: none; display: inline-flex; align-items: center; gap: 5px; font-size: 0.72rem; color: var(--muted-foreground); white-space: nowrap; }
	.pw-pres i { width: 7px; height: 7px; border-radius: 50%; background: var(--success); }
	.pw-pres.off i { background: var(--muted-foreground); }

	.pw-state { display: flex; align-items: center; gap: 6px; font-size: 0.8rem; font-weight: 600; }
	.pw-state--ok { color: var(--success); }
	.pw-state--mut { color: var(--muted-foreground); font-weight: 500; }
	.pw-state--err { color: var(--destructive); }
	.pw-state--busy { color: var(--primary); }

	.pw-kv { display: grid; grid-template-columns: 58px minmax(0, 1fr); gap: 3px 8px; margin: 0; font-size: 0.75rem; color: var(--muted-foreground); }
	.pw-kv dd { margin: 0; min-width: 0; }
	.pw-kv code { font-family: var(--font-mono, monospace); font-size: 0.72rem; color: var(--foreground); word-break: break-all; }

	.pw-bar { position: relative; height: 5px; border-radius: 99px; overflow: hidden; background: color-mix(in oklch, var(--primary) 18%, transparent); }
	.pw-bar i { display: block; height: 100%; border-radius: 99px; background: var(--primary); transition: width 400ms ease; }
	.pw-bar.indeterminate i { position: absolute; width: 35%; animation: pw-indeterminate 1.3s ease-in-out infinite; }

	.pw-hint { margin: 0; font-size: 0.72rem; line-height: 1.45; color: var(--muted-foreground); }
	.pw-hint--fg { color: var(--foreground); font-size: 0.78rem; }
	.pw-help { display: flex; gap: 6px; align-items: flex-start; margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--hover-overlay); font-size: 0.75rem; color: var(--foreground); }
	.pw-field { display: flex; flex-direction: column; gap: 4px; font-size: 0.75rem; color: var(--muted-foreground); }
	.pw-input {
		width: 100%;
		min-height: 32px;
		padding: 6px 9px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--background);
		color: var(--foreground);
		font-family: var(--font-mono, monospace);
		font-size: 0.75rem;
		outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.pw-input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }

	.pw-acts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 2px; }
	.pw-confirm { font-size: 0.75rem; color: var(--foreground); }
	.pw-empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 40px 20px; text-align: center; color: var(--muted-foreground); font-size: 0.8rem; }
	.pw-empty p { max-width: 380px; margin: 0; }
	.pw-foot { margin: 2px 0 0; font-size: 0.72rem; color: var(--muted-foreground); }

	/* Buttons: the Studio-v2 .pc-btn look (ProjectCard's are scoped there). */
	.pc-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 11px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 3px);
		font: inherit; font-size: 0.78rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, border-color 120ms ease, filter 120ms ease;
	}
	.pc-btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.pc-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.pc-btn:disabled { opacity: 0.55; cursor: not-allowed; }
	.pc-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.pc-btn--primary:hover:not(:disabled) { filter: brightness(1.08); background: var(--primary); }
	.pc-btn--ghost { background: transparent; }
	.pc-btn--danger { color: var(--destructive); border-color: color-mix(in oklab, var(--destructive) 35%, var(--border)); }
	.pc-btn--danger-text { color: var(--destructive); }
	.pc-btn--danger:hover:not(:disabled), .pc-btn--danger-text:hover:not(:disabled) { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	:global(.pw-spin) { animation: pw-spin 0.9s linear infinite; }
	:global(.pw-spin--slow) { animation-duration: 2.4s; }
	@keyframes pw-spin { to { transform: rotate(360deg); } }
	@keyframes pw-indeterminate { from { left: -35%; } to { left: 100%; } }
	@keyframes pw-shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }

	@media (max-width: 639px) {
		.pw-grid { grid-template-columns: 1fr; }
		.pc-btn { height: 36px; }
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.pw-spin), .pw-skel, .pw-bar.indeterminate i { animation: none; }
		.pw-bar.indeterminate i { left: 0; width: 100%; opacity: 0.5; }
	}
</style>
