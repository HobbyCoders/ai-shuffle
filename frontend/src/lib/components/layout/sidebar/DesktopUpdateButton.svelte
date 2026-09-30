<script lang="ts">
	import { onMount } from 'svelte';
	import { Download, RefreshCw, LoaderCircle, CircleAlert } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { notify } from '$lib/stores/notifications';

	type Worker = { device_id: string; device_name: string; phase: string; supported: boolean; online: boolean; allow_updates: boolean; version?: string; running_version?: string; percent?: number; message?: string; error?: string; reason?: string; release?: string | null; notes?: string | null };
	type Operation = { id: string; phase: 'waiting' | 'complete' | 'error'; workers: { device_id: string; device_name: string; status: string; message: string }[] };
	type Snapshot = { allow_updates: boolean; update_all_workers: boolean; workers: Worker[]; operation?: Operation | null };
	type Receipt = { results: { device_name: string; accepted: boolean; reason: string }[] };
	const tooltipId = $props.id();
	const completionKey = 'application-update-completed';
	// Poll quickly only while something is moving: a request just sent, a
	// download, or an install being verified. Otherwise the button only has to
	// notice a new update, and a hidden window does not poll at all.
	const FAST_POLL_MS = 2000;
	const IDLE_POLL_MS = 60_000;
	// The desktop updater picks a request up on its next 30 s report.
	const REQUEST_WATCH_MS = 45_000;
	let snapshot = $state<Snapshot | null>(null);
	let pending = $state(false);
	let installRequested = $state(false);
	let requestError = $state('');
	let receipt = $state('');
	let celebration = $state<'none' | 'complete' | 'leaving'>('none');
	let tipOpen = $state(false);
	let tipPosition = $state({ top: 0, left: 0 });
	let acknowledged = '';
	let active = false;
	let reading = false;
	let finishTimer: ReturnType<typeof setTimeout> | undefined;
	let hideTimer: ReturnType<typeof setTimeout> | undefined;
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let watchUntil = 0;
	const operation = $derived(snapshot?.operation);
	const waiting = $derived(operation?.phase === 'waiting' || installRequested);
	const celebrating = $derived(celebration !== 'none');
	const eligible = $derived(snapshot?.workers.filter(w => w.supported && w.online && w.allow_updates &&
		!(w.version && w.running_version && (w.running_version.startsWith(w.version) || w.version.startsWith(w.running_version)))) ?? []);
	const downloading = $derived(eligible.some(w => ['checking', 'downloading'].includes(w.phase)));
	const available = $derived(eligible.some(w => ['available', 'error'].includes(w.phase) && !!w.version));
	const ready = $derived(!available && !downloading && eligible.some(w => w.phase === 'ready'));
	// Release notes travel in the signed manifest; show the offered update's.
	const offered = $derived(eligible.find(w => ['available', 'downloading', 'ready'].includes(w.phase) && !!w.notes?.trim()));
	const operationError = $derived(operation?.phase === 'error' ? 'Update startup could not be confirmed. Check the affected workers before retrying.' : '');
	const error = $derived(requestError || operationError || eligible.find(w => w.error)?.error || '');
	const visible = $derived(!!snapshot?.allow_updates && (waiting || celebrating || ready || available || downloading || !!receipt || !!error));
	const busy = $derived(pending || downloading || waiting || celebrating);
	const action = $derived(ready ? 'Update and restart' : available ? 'Download update' : 'Check for updates');
	const scope = $derived(snapshot?.update_all_workers ? 'All online workers' : 'Current worker');
	const reports = $derived(operation && (waiting || celebrating || operationError)
		? operation.workers.map(w => `${w.device_name}: ${w.message}`).join('; ')
		: snapshot?.workers.map(w => `${w.device_name}: ${w.reason || w.error || w.message || w.phase}`).join('; ') ?? '');
	const headline = $derived(celebrating ? 'Update complete' : waiting ? 'Waiting on all workers to finish startup' : error ? 'Update needs attention' : busy ? 'Preparing updates…' : action);
	const status = $derived(waiting || celebrating ? `${headline}. ${reports}` : `${scope}: ${error || receipt || (busy ? 'Preparing updates…' : ready ? 'Update ready. Click to install and restart.' : 'Update available. Click to download.')} ${reports}`);

	function showTip(event: MouseEvent | FocusEvent) {
		const rect = (event.currentTarget as HTMLButtonElement).getBoundingClientRect();
		tipPosition = { top: rect.bottom + 12, left: Math.max(8, Math.min(rect.left - 170, window.innerWidth - 288)) };
		tipOpen = true;
	}
	function applySnapshot(next: Snapshot) {
		snapshot = next;
		if (!pending) installRequested = false;
		if (next.operation?.phase !== 'complete') return;
		receipt = ''; requestError = '';
		if (acknowledged === next.operation.id) return;
		acknowledged = next.operation.id;
		try { localStorage.setItem(completionKey, acknowledged); } catch { /* Still dedupe within this mount. */ }
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
	async function activate(announce = false) {
		if (busy || !snapshot?.allow_updates) return;
		const requestedAction = ready ? 'install' : available ? 'download' : 'check';
		watchUntil = Date.now() + REQUEST_WATCH_MS;
		pending = true; installRequested = requestedAction === 'install'; requestError = ''; receipt = '';
		try {
			const result = await api.post<Receipt>('/settings/application-updates', { action: requestedAction });
			if (!active) return;
			receipt = result.results.map(w => `${w.device_name}: ${w.reason}`).join('; ');
			if (!result.results.some(w => w.accepted)) installRequested = false;
			if (announce) {
				const message = `${scope}. ${receipt}${requestedAction === 'download' ? ' When preparation finishes, choose Update and restart to install.' : ''}`;
				if (result.results.some(w => !w.accepted)) notify.warning(message, { title: 'Update needs attention', persist: false });
				else notify.info(message, { title: 'Update requested', persist: false });
			}
		} catch (cause) {
			if (active) {
				requestError = getErrorMessage(cause, 'Could not request application update');
				if (announce) notify.error(requestError);
			}
		}
		finally { if (active) { pending = false; void refresh(); } }
	}
</script>

{#if visible}
	<div class="update-slot" class:complete={celebrating} class:leaving={celebration === 'leaving'}>
		<button class="update-button" class:ready class:failed={!!error && !waiting && !celebrating}
			type="button" onclick={() => activate()} aria-disabled={busy} aria-busy={busy && !celebrating}
			aria-label={waiting || celebrating ? headline : busy ? 'Downloading update' : action}
			title={status} aria-describedby={tipOpen ? tooltipId : undefined}
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
		{#if tipOpen}
			<div class="update-tooltip" id={tooltipId} role="tooltip" style:top={`${tipPosition.top}px`} style:left={`${tipPosition.left}px`}>
				<strong>{headline}</strong>
				{#if operation && (waiting || celebrating || operationError)}
					{#each operation.workers as worker (worker.device_id)}
						<div class="worker-row"><span>{worker.device_name}</span><span class:verified={worker.status === 'verified'}>{worker.message}</span></div>
					{/each}
				{:else}<span>{status}</span>{/if}
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
	.update-slot { width: 28px; height: 28px; position: relative; flex: none; -webkit-app-region: no-drag; }
	.update-button { width: 100%; height: 100%; flex: none; display: grid; place-items: center; position: relative; border: none; border-radius: 8px; color: var(--primary); background: color-mix(in oklch, var(--primary) 12%, transparent); cursor: pointer; }
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
	@keyframes update-spin { to { transform: rotate(360deg); } }
	@keyframes ring-out { to { opacity: 0; transform: scale(.5) rotate(90deg); } }
	@keyframes check-in { from { opacity: 1; stroke-dashoffset: 25; } to { opacity: 1; stroke-dashoffset: 0; } }
	@keyframes update-pop { 0%,100% { transform: scale(1); } 28% { transform: scale(.88); } 62% { transform: scale(1.18); } 82% { transform: scale(.97); } }
	@keyframes update-spark { 0% { opacity: 0; transform: rotate(var(--angle)) translateY(-12px) scale(.6); } 20% { opacity: 1; } 100% { opacity: 0; transform: rotate(var(--angle)) translateY(-26px) scale(.1); } }
	@keyframes update-halo { from { opacity: .7; transform: scale(.95); } to { opacity: 0; transform: scale(1.7); } }
	@keyframes update-leave { to { opacity: 0; transform: scale(.65); } }
	@keyframes close-gap { to { width: 0; margin-left: -8px; } }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
	@media (hover: none) { .update-slot { width: 44px; height: 44px; } }
	@media (prefers-reduced-motion: reduce) {
		.update-button :global(.update-spinner), .completion-ring, .complete .update-button, .leaving { animation: none; }
		.complete .completion-ring { animation: none; opacity: 0; }
		.complete .completion-check { animation: none; opacity: 1; stroke-dashoffset: 0; }
		.spark, .halo { display: none; }
		.leaving .update-button { animation: none; opacity: 0; }
	}
</style>
