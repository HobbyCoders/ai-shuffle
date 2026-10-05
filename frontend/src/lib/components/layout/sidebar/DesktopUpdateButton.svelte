<script lang="ts">
	import { onMount } from 'svelte';
	import { Download, RefreshCw, LoaderCircle, CircleAlert } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { notify } from '$lib/stores/notifications';
	import NotificationSlot from '$lib/components/NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	type Worker = { device_id: string; device_name: string; is_self?: boolean; phase: string; supported: boolean; online: boolean; allow_updates: boolean; version?: string; running_version?: string; percent?: number; message?: string; error?: string; reason?: string; release?: string | null; notes?: string | null; request_pending?: boolean; channel?: string | null; checked_at?: number | null; role?: Role; label?: string; deferred?: boolean };
	type Operation = { id: string; phase: 'waiting' | 'complete' | 'error'; completed_at?: number; workers: { device_id: string; device_name: string; status: string; message: string }[] };
	type Snapshot = { allow_updates: boolean; update_all_workers: boolean; server_time?: number; workers: Worker[]; operation?: Operation | null };
	// excluded: not part of this update (offline, no updater, disabled, current, nothing offered).
	// unknown: an included worker whose state cannot be trusted; restart waits for it.
	type Role = 'excluded' | 'preparing' | 'available' | 'staged' | 'unknown';
	/** A chat still running on a worker that refused to restart (#1818). */
	type Busy = { session_id: string; title: string; tasks: string[] };
	type Receipt = { results: { device_id?: string; device_name: string; accepted: boolean | null; reason: string; busy?: Busy[]; deferred?: boolean }[] };
	/** The restart panel: work that kept a restart from running, or a restart waiting on it. */
	type Panel = { kind: 'busy'; workers: { device_name: string; busy: Busy[] }[] } | { kind: 'deferred' };
	// Older container supervisors leave "Application update is running." on an
	// idle status after an install; a message like that does not fit idle.
	const IN_PROGRESS = /\b(?:update is running|checking|downloading|building|preparing|installing)\b/i;
	/** `canCheck` tells the host whether a manual "Check for updates" entry
	 *  makes sense right now: updates are allowed and this is not an agent
	 *  preview. The button itself only appears once there is something to show. */
	let { canCheck = $bindable(false) }: { canCheck?: boolean } = $props();
	const tooltipId = $props.id();
	const completionKey = 'application-update-completed';
	// Poll quickly only while something is moving: a request just sent, a
	// download, or an install being verified. Otherwise the button only has to
	// notice a new update, and a hidden window does not poll at all.
	const FAST_POLL_MS = 2000;
	const IDLE_POLL_MS = 60_000;
	// The desktop updater picks a request up on its next 30 s report.
	const REQUEST_WATCH_MS = 45_000;
	// A completion older than this is history: a fresh browser must not pop it.
	const CELEBRATE_WINDOW_S = 10 * 60;
	let snapshot = $state<Snapshot | null>(null);
	let pending = $state(false);
	let installRequested = $state(false);
	let requestError = $state('');
	let receipt = $state('');
	let celebration = $state<'none' | 'complete' | 'leaving'>('none');
	// Reasons workers gave for refusing the last request, by device id.
	let refusals = $state<Record<string, string>>({});
	let tipOpen = $state(false);
	let tipPosition = $state({ top: 0, left: 0 });
	let panel = $state<Panel | null>(null);
	let buttonEl = $state<HTMLButtonElement | null>(null);
	const panelId = `${tooltipId}-panel`;
	let acknowledged = '';
	let watched = '';
	let active = false;
	let reading = false;
	let finishTimer: ReturnType<typeof setTimeout> | undefined;
	let hideTimer: ReturnType<typeof setTimeout> | undefined;
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let watchUntil = 0;
	// A manual check reports its outcome once: an offered update shows the
	// button, and nothing found says so instead of leaving the user guessing.
	let manualCheckAt = 0;
	// A worker picks a check up within seconds; never call a check finished sooner.
	const MANUAL_CHECK_SETTLE_MS = 5000;
	const operation = $derived(snapshot?.operation);
	// A worker waiting to restart until its running work ends ("Restart when they finish").
	const deferred = $derived(!!snapshot?.workers.some(w => w.deferred));
	const waiting = $derived(operation?.phase === 'waiting' || installRequested);
	const celebrating = $derived(celebration !== 'none');
	// The worker classifies (app/core/update_workers.py classify()); the copy
	// below only covers a snapshot without roles.
	const offers = $derived(offeredUpdates(snapshot));
	const rows = $derived(snapshot?.workers.map(worker => ({ worker, ...(worker.role && typeof worker.label === 'string'
		? { role: worker.role, label: worker.label } : classify(worker, snapshot!.update_all_workers, offers)) })) ?? []);
	const targets = $derived(rows.filter(row => row.role !== 'excluded'));
	const downloading = $derived(targets.some(row => row.role === 'preparing'));
	const available = $derived(targets.some(row => row.role === 'available'));
	const staged = $derived(targets.filter(row => row.role === 'staged').length);
	const unknown = $derived(targets.filter(row => row.role === 'unknown'));
	// Restart is offered only once EVERY included worker is staged, never on the first one.
	const ready = $derived(staged > 0 && !downloading && !available && !unknown.length);
	const held = $derived(staged > 0 && !downloading && !available && unknown.length
		? `Restart waits for ${unknown.map(row => `${row.worker.device_name} (${row.label})`).join(', ')}.` : '');
	// Release notes travel in the signed manifest; show the offered update's.
	const offered = $derived(targets.find(row => row.role !== 'unknown' && !!row.worker.notes?.trim())?.worker);
	const operationError = $derived(operation?.phase === 'error' ? 'Update startup could not be confirmed. Check the affected workers before retrying.' : '');
	const error = $derived(requestError || operationError || targets.find(row => row.worker.error)?.worker.error || held);
	const visible = $derived(!!snapshot?.allow_updates && (waiting || celebrating || ready || available || downloading || !!receipt || !!error));
	const busy = $derived(pending || downloading || waiting || celebrating);
	const action = $derived(ready ? 'Update and restart' : available ? 'Download update' : 'Check for updates');
	const scope = $derived(snapshot?.update_all_workers ? 'All online workers' : 'Current worker');
	const showOperation = $derived(!!operation && (waiting || celebrating || !!operationError));
	const reports = $derived(showOperation
		? operation!.workers.map(w => `${w.device_name}: ${w.message}`).join('; ')
		: rows.map(row => `${row.worker.device_name}: ${row.label}`).join('; '));
	const headline = $derived(celebrating ? 'Update complete' : waiting ? 'Waiting on all workers to finish startup' : deferred ? 'Restarts when work finishes' : error ? 'Update needs attention' : busy ? 'Preparing updates…' : action);
	const progressNote = $derived(targets.length > 1 ? ` ${staged} of ${targets.length} staged.` : '');
	const summary = $derived(error || (busy ? `Preparing updates…${progressNote}` : ready ? 'Update ready. Click to install and restart.' : 'Update available. Click to download.'));
	const status = $derived(waiting || celebrating ? `${headline}. ${reports}` : `${scope}: ${summary}${receipt ? ` Last request: ${receipt}.` : ''} ${reports}`);
	$effect(() => { canCheck = !!snapshot?.allow_updates; });

	const same = (a?: string | null, b?: string | null) => !!a && !!b && (a.startsWith(b) || b.startsWith(a));
	type Offer = { version: string; channel?: string | null; checked_at?: number | null };
	function offeredUpdates(next: Snapshot | null): Offer[] {
		return (next?.workers ?? []).filter(w => (next!.update_all_workers || w.is_self !== false) && w.online && w.supported && w.allow_updates
			&& ['available', 'downloading', 'ready'].includes(w.phase) && !!w.version && !same(w.version, w.running_version))
			.map(w => ({ version: w.version!, channel: w.channel, checked_at: w.checked_at }));
	}
	// An idle worker that has not read its feed since another worker found an
	// update has simply not seen it yet (containers check once a day). A worker
	// that does not report checked_at predates this rule and is left out of it.
	function notChecked(w: Worker, found: Offer[]): boolean {
		if (w.checked_at === undefined) return false;
		return found.some(offer => !same(offer.version, w.running_version)
			&& !(offer.channel && w.channel && offer.channel !== w.channel)
			&& (typeof w.checked_at !== 'number' || (typeof offer.checked_at === 'number' && w.checked_at < offer.checked_at)));
	}

	function classify(w: Worker, all: boolean, found: Offer[] = []): { role: Role; label: string } {
		// A peer read only to verify an earlier all-worker install is outside a current-worker update.
		if (!all && w.is_self === false) return { role: 'excluded', label: 'Not in scope; current worker only' };
		if (!w.online) return { role: 'excluded', label: w.reason || 'Offline; not included' };
		// A failed status read is not proof the worker sits this update out.
		if (w.phase === 'unknown') return { role: 'unknown', label: 'Status unavailable' };
		if (!w.supported) return { role: 'excluded', label: w.reason || 'No application updater; not included' };
		if (!w.allow_updates) return { role: 'excluded', label: 'Updates disabled on this worker; not included' };
		if (w.version && w.running_version && (w.running_version.startsWith(w.version) || w.version.startsWith(w.running_version))) {
			return { role: 'excluded', label: 'Already up to date' };
		}
		// Queued but not yet taken (a desktop takes requests on its 30 s report).
		if (w.request_pending && w.phase !== 'ready') return { role: 'preparing', label: 'Request queued; waiting for the updater' };
		// A worker that refused the last request and is not preparing or staged
		// would be left behind; restart waits until a new request reaches it.
		const refused = refusals[w.device_id];
		if (refused && !['checking', 'downloading', 'ready', 'image-required'].includes(w.phase)) return { role: 'unknown', label: `Refused: ${refused}` };
		switch (w.phase) {
			case 'checking': return { role: 'preparing', label: 'Checking for updates…' };
			case 'downloading': return { role: 'preparing', label: `${w.message || 'Downloading update…'}${typeof w.percent === 'number' ? ` ${Math.round(w.percent)}%` : ''}` };
			case 'ready': return { role: 'staged', label: w.error ? `Staged, awaiting restart. ${w.error}` : 'Staged, awaiting restart' };
			case 'available': return w.version ? { role: 'available', label: 'Update available; not downloaded' } : { role: 'excluded', label: 'No update offered' };
			case 'error': return w.version ? { role: 'available', label: w.error || 'Download failed; retry' } : { role: 'unknown', label: w.error || 'Update check failed' };
			case 'image-required': return { role: 'excluded', label: w.message || 'Needs a new Docker image; not included' };
			case 'idle': if (notChecked(w, found)) return { role: 'available', label: 'Not checked yet; will check and download' };
			// fall through
			default: return { role: 'excluded', label: (w.message && !IN_PROGRESS.test(w.message) ? w.message : '') || 'No update offered' };
		}
	}

	function anchor(rect: DOMRect) {
		return { top: rect.bottom + 12, left: Math.max(8, Math.min(rect.left - 170, window.innerWidth - 288)) };
	}
	function showTip(event: MouseEvent | FocusEvent) {
		if (panel) return;
		tipPosition = anchor((event.currentTarget as HTMLButtonElement).getBoundingClientRect());
		tipOpen = true;
	}
	/** The restart notice draws in the shared notification dock (#1823), not under this button. */
	function openPanel(next: Panel) {
		tipOpen = false;
		panel = next;
	}
	function closePanel() { panel = null; }
	function panelKey(event: KeyboardEvent) {
		if (panel && event.key === 'Escape') { closePanel(); buttonEl?.focus(); }
	}
	/** Plain text of what is running, for the bell: "Fix login (Run tests); Update docs". */
	function busyText(workers: { device_name: string; busy: Busy[] }[]): string {
		return workers.map(w => `${workers.length > 1 ? `${w.device_name}: ` : ''}${w.busy.map(b => b.tasks.length ? `${b.title} (${b.tasks.join(', ')})` : b.title).join('; ')}`).join('. ');
	}
	/** A restart refused because work is running: say so at once, here and in the bell (#1818). */
	function refusedForWork(result: Receipt): boolean {
		const workers = result.results.filter(w => w.accepted === false && w.busy?.length).map(w => ({ device_name: w.device_name, busy: w.busy! }));
		if (!workers.length) return false;
		openPanel({ kind: 'busy', workers });
		notify.warning(`Restart would stop running work: ${busyText(workers)}.`, {
			title: 'Restart is waiting', persist: false,
			action: { label: 'Restart when they finish', onClick: () => void restartLater('when_idle') },
			secondaryAction: { label: 'Stop them and restart', onClick: () => void restartLater('stop') }
		});
		return true;
	}
	/** "Restart when they finish" or "Stop them and restart". */
	async function restartLater(mode: 'when_idle' | 'stop') {
		closePanel();
		if (pending) return;
		pending = true; requestError = '';
		try {
			const result = await api.post<Receipt>('/settings/application-updates', { action: 'install', mode });
			if (!active) return;
			const refused = result.results.filter(w => !w.accepted);
			receipt = refused.map(w => `${w.device_name}: ${w.reason}`).join('; ');
			refusals = Object.fromEntries(refused.filter(w => w.device_id).map(w => [w.device_id!, w.reason]));
			if (refused.length) notify.warning(receipt, { title: 'Update needs attention', persist: false });
			else notify.info(mode === 'stop' ? 'Stopping running work, then restarting.' : 'Restarts when running work finishes.',
				{ title: mode === 'stop' ? 'Restarting' : 'Restart scheduled', persist: false });
		} catch (cause) {
			if (active) { requestError = getErrorMessage(cause, 'Could not schedule the restart'); notify.error(requestError); }
		} finally { if (active) { pending = false; watchUntil = Date.now() + REQUEST_WATCH_MS; void refresh(); } }
	}
	async function cancelRestart() {
		closePanel();
		if (pending) return;
		pending = true;
		try {
			await api.post<Receipt>('/settings/application-updates', { action: 'cancel' });
			if (active) notify.info('The update stays staged.', { title: 'Restart cancelled', persist: false });
		} catch (cause) {
			if (active) notify.error(getErrorMessage(cause, 'Could not cancel the restart'));
		} finally { if (active) { pending = false; void refresh(); } }
	}
	function clicked() {
		if (panel) { closePanel(); return; }
		if (deferred && !busy) { openPanel({ kind: 'deferred' }); return; }
		void activate();
	}
	function applySnapshot(next: Snapshot) {
		snapshot = next;
		if (!pending) installRequested = false;
		const done = next.operation;
		if (done?.phase === 'waiting') watched = done.id;
		settleManualCheck(next);
		if (done?.phase !== 'complete') return;
		receipt = ''; requestError = ''; refusals = {};
		if (acknowledged === done.id) return;
		acknowledged = done.id;
		try { localStorage.setItem(completionKey, acknowledged); } catch { /* Still dedupe within this mount. */ }
		// Celebrate only the install this view watched finish, or one that just
		// finished (the app restarted into it). Never replay an older completion
		// while a newer update is staged or being prepared.
		const recent = watched === done.id || (typeof done.completed_at === 'number' && typeof next.server_time === 'number'
			&& next.server_time - done.completed_at < CELEBRATE_WINDOW_S);
		if (!recent || staged || available || downloading) return;
		clearTimeout(finishTimer); clearTimeout(hideTimer);
		celebration = 'complete';
		finishTimer = setTimeout(() => { celebration = 'leaving'; }, 1850);
		hideTimer = setTimeout(() => { celebration = 'none'; tipOpen = false; }, 2150);
	}
	function schedule() {
		clearTimeout(pollTimer); pollTimer = undefined;
		if (!active || document.hidden) return;
		const moving = pending || waiting || downloading || Date.now() < watchUntil;
		pollTimer = setTimeout(refresh, moving ? FAST_POLL_MS : IDLE_POLL_MS);
	}
	async function refresh() {
		if (reading) return;
		if (auth.current.status?.preview_access) { snapshot = null; schedule(); return; }
		reading = true;
		try {
			const next = await api.get<Snapshot>('/settings/application-updates/workers');
			if (active) applySnapshot(next);
		} catch { /* Keep waiting through the worker's restart; never offer a stale install. */ }
		finally { reading = false; schedule(); }
	}
	function visibilityChanged() {
		if (document.hidden) { clearTimeout(pollTimer); pollTimer = undefined; } else void refresh();
	}
	onMount(() => {
		active = true;
		try { acknowledged = localStorage.getItem(completionKey) || ''; } catch { /* Storage may be unavailable. */ }
		void refresh();
		window.addEventListener('application-update-policy', refresh);
		window.addEventListener('application-update-request', activateFromNotification);
		document.addEventListener('visibilitychange', visibilityChanged);
		return () => {
			active = false; clearTimeout(pollTimer); clearTimeout(finishTimer); clearTimeout(hideTimer);
			window.removeEventListener('application-update-policy', refresh);
			window.removeEventListener('application-update-request', activateFromNotification);
			document.removeEventListener('visibilitychange', visibilityChanged);
		};
	});
	async function activateFromNotification() {
		// Never fall back to the shell's local-only install when policy cannot
		// be read. A ready desktop may still have peers that need preparation.
		if (auth.current.status?.preview_access) {
			notify.error('Application updates cannot be requested from an agent preview.');
			return;
		}
		if (pending) return;
		pending = true;
		try {
			const next = await api.get<Snapshot>('/settings/application-updates/workers');
			if (!active) return;
			applySnapshot(next);
		} catch (cause) {
			notify.error(getErrorMessage(cause, 'Could not check application update scope. Try again.'));
			return;
		} finally { pending = false; }
		if (!snapshot?.allow_updates) {
			notify.error('Application updates are disabled in Settings.');
			return;
		}
		if (busy) {
			notify.info(status, { title: 'Update in progress', persist: false });
			return;
		}
		await activate(true);
	}
	function settleManualCheck(next: Snapshot) {
		if (!manualCheckAt) return;
		const elapsed = Date.now() - manualCheckAt;
		const inScope = rows.filter(row => row.role !== 'excluded');
		const checking = next.workers.some(w => w.phase === 'checking' || w.request_pending);
		const found = available || staged > 0 || inScope.some(row => row.role === 'preparing' && row.worker.phase === 'downloading');
		if (found) {
			manualCheckAt = 0;
			notify.info(summary, { title: ready ? 'Update ready' : 'Update found', persist: false });
			return;
		}
		if (elapsed < MANUAL_CHECK_SETTLE_MS || (checking && elapsed < REQUEST_WATCH_MS)) return;
		manualCheckAt = 0;
		if (checking) notify.info('Still checking. The update button appears here if an update is found.', { title: 'Checking for updates', persist: false });
		else if (error) notify.warning(error, { title: 'Update check needs attention', persist: false });
		else notify.info(snapshot?.update_all_workers ? 'Every included worker is up to date.' : 'This worker is up to date.', { title: 'No updates found', persist: false });
	}
	/** The manual "Check for updates" entry (#1623). Asks every worker in scope
	 *  to read its feed now instead of waiting for the daily check. */
	export async function checkForUpdates() {
		if (auth.current.status?.preview_access) {
			notify.error('Application updates cannot be requested from an agent preview.');
			return;
		}
		if (!snapshot?.allow_updates) {
			notify.error('Application updates are disabled in Settings.');
			return;
		}
		if (busy) {
			notify.info(status, { title: 'Update in progress', persist: false });
			return;
		}
		if (ready || available) {
			notify.info(summary, { title: ready ? 'Update ready' : 'Update available', persist: false });
			return;
		}
		const result = await activate(false, 'check');
		if (!result) {
			if (requestError) notify.error(requestError);
			return;
		}
		if (result.results.some(w => w.accepted)) {
			manualCheckAt = Date.now();
			notify.info('Asking each worker in scope for its latest version.', { title: 'Checking for updates', persist: false });
		} else {
			notify.warning(receipt || 'No worker accepted the check.', { title: 'Update check needs attention', persist: false });
		}
	}
	async function activate(announce = false, forced?: 'check'): Promise<Receipt | null> {
		if (busy || !snapshot?.allow_updates) return null;
		const requestedAction = forced ?? (ready ? 'install' : available ? 'download' : 'check');
		watchUntil = Date.now() + REQUEST_WATCH_MS;
		pending = true; installRequested = requestedAction === 'install'; requestError = ''; receipt = ''; refusals = {};
		try {
			const result = await api.post<Receipt>('/settings/application-updates', { action: requestedAction });
			if (!active) return null;
			// Keep only refusals: accepted work shows up in each worker's live phase.
			const refused = result.results.filter(w => !w.accepted);
			receipt = refused.map(w => `${w.device_name}: ${w.reason}`).join('; ');
			refusals = Object.fromEntries(refused.filter(w => w.device_id).map(w => [w.device_id!, w.reason]));
			if (!result.results.some(w => w.accepted)) installRequested = false;
			if (requestedAction === 'install' && refusedForWork(result)) return result;
			if (announce) {
				const message = `${scope}. ${result.results.map(w => `${w.device_name}: ${w.reason}`).join('; ')}${requestedAction === 'download' ? ' When preparation finishes, choose Update and restart to install.' : ''}`;
				if (result.results.some(w => !w.accepted)) notify.warning(message, { title: 'Update needs attention', persist: false });
				else notify.info(message, { title: 'Update requested', persist: false });
			}
			return result;
		} catch (cause) {
			if (active) {
				requestError = getErrorMessage(cause, 'Could not request application update');
				if (announce) notify.error(requestError);
			}
			return null;
		}
		finally { if (active) { pending = false; void refresh(); } }
	}
</script>

<svelte:window onkeydown={panelKey} />

{#if visible}
	<div class="update-slot" class:complete={celebrating} class:leaving={celebration === 'leaving'}>
		<button class="update-button" class:ready class:failed={!!error && !waiting && !celebrating && !deferred}
			type="button" onclick={clicked} aria-disabled={busy} aria-busy={busy && !celebrating} bind:this={buttonEl}
			aria-label={waiting || celebrating || deferred ? headline : busy ? 'Downloading update' : action}
			aria-haspopup="dialog" aria-expanded={!!panel} aria-controls={panel ? panelId : undefined}
			title={panel ? undefined : status} aria-describedby={tipOpen ? tooltipId : undefined}
			onmouseenter={showTip} onmouseleave={() => tipOpen = false} onfocus={showTip} onblur={() => tipOpen = false}
			onkeydown={(event) => { if (event.key === 'Escape') tipOpen = false; }}>
			{#if waiting || celebrating}
				<svg class="completion-ring" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" aria-hidden="true"><path d="M12 3a9 9 0 1 1-6.36 2.64" /></svg>
				<svg class="completion-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4.5 4.5L19 7" /></svg>
				<span class="halo" aria-hidden="true"></span>
				<span class="burst" aria-hidden="true">{#each Array(8) as _, i}<i class="spark" style={`--angle:${i * 45 + 12}deg`}></i>{/each}</span>
			{:else if busy}<LoaderCircle size={16} strokeWidth={1.85} class="update-spinner" />
			{:else if error}<CircleAlert size={16} strokeWidth={1.85} />
			{:else if ready}<RefreshCw size={16} strokeWidth={1.85} />
			{:else}<Download size={16} strokeWidth={1.85} />{/if}
		</button>
		{#if panel}
			<NotificationSlot order={DOCK_ORDER.restart}>
			<div class="update-panel" id={panelId} role="dialog" aria-label={panel.kind === 'busy' ? 'Work is still running' : 'Restart scheduled'}>
				{#if panel.kind === 'busy'}
					<strong>Work is still running</strong>
					<p class="panel-hint">Restart would stop it.</p>
					{#each panel.workers as group (group.device_name)}
						{#if panel.workers.length > 1}<span class="panel-worker">{group.device_name}</span>{/if}
						<ul class="busy-list">
							{#each group.busy as item (item.session_id)}
								<li><span class="busy-title">{item.title}</span>{#if item.tasks.length}<span class="busy-tasks">{item.tasks.join(', ')}</span>{/if}</li>
							{/each}
						</ul>
					{/each}
					<div class="panel-actions">
						<button type="button" class="panel-primary" onclick={() => restartLater('when_idle')}>Restart when they finish</button>
						<button type="button" class="panel-danger" onclick={() => restartLater('stop')}>Stop them and restart</button>
						<button type="button" class="panel-quiet" onclick={closePanel}>Not now</button>
					</div>
				{:else}
					<strong>Restart scheduled</strong>
					<p class="panel-hint">Restarts when work finishes.</p>
					<div class="panel-actions">
						<button type="button" class="panel-danger" onclick={cancelRestart}>Cancel restart</button>
						<button type="button" class="panel-quiet" onclick={closePanel}>Close</button>
					</div>
				{/if}
			</div>
			</NotificationSlot>
		{/if}
		{#if tipOpen}
			<div class="update-tooltip" id={tooltipId} role="tooltip" style:top={`${tipPosition.top}px`} style:left={`${tipPosition.left}px`}>
				<strong>{headline}</strong>
				{#if operation && showOperation}
					{#each operation.workers as worker (worker.device_id)}
						<div class="worker-row"><span>{worker.device_name}</span><span class:verified={worker.status === 'verified'}>{worker.message}</span></div>
					{/each}
				{:else}
					<span>{scope}: {summary}</span>
					{#each rows as row (row.worker.device_id)}
						<div class="worker-row"><span>{row.worker.device_name}</span><span class:verified={row.role === 'staged'}>{row.label}</span></div>
					{/each}
					{#if receipt}<p>Last request: {receipt}</p>{/if}
				{/if}
				{#if offered && !waiting && !celebrating}
					<div class="release-notes">
						<strong>What's new{offered.release ? ` in ${offered.release}` : ''}</strong>
						<div class="notes-text">{offered.notes}</div>
					</div>
				{/if}
				{#if waiting}<p>Checking that every worker included in this update has the new version.</p>{/if}
			</div>
		{/if}
	</div>
	<span class="sr-only" role={error && !waiting ? 'alert' : 'status'}>{status}</span>
{/if}

<style>
	.update-slot { width: 32px; height: 32px; position: relative; flex: none; -webkit-app-region: no-drag; }
	.update-button { width: 100%; height: 100%; flex: none; display: grid; place-items: center; position: relative; border: none; border-radius: 9px; color: var(--primary); background: color-mix(in oklch, var(--primary) 12%, transparent); cursor: pointer; }
	.update-button:hover { background: color-mix(in oklch, var(--primary) 22%, transparent); }
	.update-button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.update-button[aria-disabled='true'] { cursor: wait; }
	.update-button.ready { box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 35%, transparent); }
	.update-button.failed { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.update-button :global(.update-spinner), .completion-ring { animation: update-spin 1s linear infinite; }
	.completion-ring, .completion-check { position: absolute; }
	.completion-check { opacity: 0; stroke-dasharray: 25; stroke-dashoffset: 25; }
	.burst, .halo { position: absolute; inset: 0; pointer-events: none; }
	.halo { border: 1px solid currentColor; border-radius: 9px; opacity: 0; }
	.spark { position: absolute; left: calc(50% - 1px); top: calc(50% - 2px); width: 2px; height: 4px; border-radius: 2px; background: currentColor; opacity: 0; }
	.spark:nth-child(3n) { height: 2px; }
	.complete .update-button { animation: update-pop .52s cubic-bezier(.2,.8,.2,1) both; cursor: default; }
	.complete .completion-ring { animation: ring-out .2s ease-out forwards; }
	.complete .completion-check { animation: check-in .32s .1s ease-out forwards; }
	.complete .spark { animation: update-spark .65s .14s ease-out both; }
	.complete .halo { animation: update-halo .6s .12s ease-out both; }
	.leaving { animation: close-gap .3s ease-in forwards; }
	.leaving .update-button { animation: update-leave .3s ease-in forwards; }
	.update-tooltip { position: fixed; z-index: 250; width: 280px; max-width: calc(100vw - 16px); padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--popover); color: var(--popover-foreground); box-shadow: 0 8px 24px #0004; font-size: 11px; line-height: 1.5; pointer-events: none; overflow-wrap: anywhere; }
	.update-tooltip strong { display: block; font-size: 12px; margin-bottom: 8px; }
	.worker-row { display: flex; justify-content: space-between; gap: 12px; margin-top: 6px; }
	.worker-row span:last-child { text-align: right; color: var(--muted-foreground); }
	.worker-row .verified { color: var(--primary) !important; }
	.release-notes { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); }
	.release-notes strong { margin-bottom: 4px; }
	.notes-text { white-space: pre-line; color: var(--muted-foreground); display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 12; line-clamp: 12; overflow: hidden; }
	.update-tooltip p { margin: 10px 0 0; padding-top: 8px; border-top: 1px solid var(--border); color: var(--muted-foreground); }
	.update-panel { width: 100%; max-width: 100%; padding: 12px 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--card); color: var(--card-foreground, var(--foreground)); box-shadow: var(--shadow-l); font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
	.update-panel strong { display: block; font-size: 14px; }
	.panel-hint { margin: 2px 0 8px; color: var(--muted-foreground); }
	.panel-worker { display: block; margin-top: 6px; font-size: 11px; color: var(--muted-foreground); }
	.busy-list { margin: 4px 0 0; padding: 0; list-style: none; max-height: 160px; overflow-y: auto; }
	.busy-list li { display: flex; flex-direction: column; padding: 5px 8px; border-radius: 6px; background: color-mix(in oklch, var(--muted) 60%, transparent); }
	.busy-list li + li { margin-top: 4px; }
	.busy-title { color: var(--foreground); }
	.busy-tasks { font-size: 11px; color: var(--muted-foreground); }
	.panel-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
	.panel-actions button { min-height: 32px; padding: 0 10px; border-radius: 7px; border: 1px solid transparent; font-size: 12px; text-align: center; cursor: pointer; }
	.panel-actions button:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.panel-primary { color: var(--primary-foreground); background: var(--primary); }
	.panel-primary:hover { background: color-mix(in oklch, var(--primary) 88%, var(--foreground)); }
	.panel-actions .panel-danger { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 10%, transparent); border-color: color-mix(in oklch, var(--destructive) 30%, transparent); }
	.panel-danger:hover { background: color-mix(in oklch, var(--destructive) 18%, transparent); }
	.panel-quiet { color: var(--muted-foreground); background: transparent; }
	.panel-quiet:hover { background: var(--hover-overlay, color-mix(in oklch, var(--muted) 60%, transparent)); color: var(--foreground); }
	@keyframes update-spin { to { transform: rotate(360deg); } }
	@keyframes ring-out { to { opacity: 0; transform: scale(.5) rotate(90deg); } }
	@keyframes check-in { from { opacity: 1; stroke-dashoffset: 25; } to { opacity: 1; stroke-dashoffset: 0; } }
	@keyframes update-pop { 0%,100% { transform: scale(1); } 28% { transform: scale(.88); } 62% { transform: scale(1.18); } 82% { transform: scale(.97); } }
	@keyframes update-spark { 0% { opacity: 0; transform: rotate(var(--angle)) translateY(-12px) scale(.6); } 20% { opacity: 1; } 100% { opacity: 0; transform: rotate(var(--angle)) translateY(-26px) scale(.1); } }
	@keyframes update-halo { from { opacity: .7; transform: scale(.95); } to { opacity: 0; transform: scale(1.7); } }
	@keyframes update-leave { to { opacity: 0; transform: scale(.65); } }
	@keyframes close-gap { to { width: 0; margin-left: calc(-1 * var(--update-gap, 8px)); } }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
	@media (max-width: 639px), (pointer: coarse) { .panel-actions button { min-height: 44px; } }
	@media (hover: none) { .update-slot { width: 40px; height: 40px; } .update-button { border-radius: 11px; } }
	@media (prefers-reduced-motion: reduce) {
		.update-button :global(.update-spinner), .completion-ring, .complete .update-button, .leaving { animation: none; }
		.complete .completion-ring { animation: none; opacity: 0; }
		.complete .completion-check { animation: none; opacity: 1; stroke-dashoffset: 0; }
		.spark, .halo { display: none; }
		.leaving .update-button { animation: none; opacity: 0; }
	}
</style>
