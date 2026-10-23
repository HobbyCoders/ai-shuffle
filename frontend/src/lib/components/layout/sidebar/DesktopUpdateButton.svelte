<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { Download, RefreshCw, LoaderCircle, CircleAlert } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { notify } from '$lib/stores/notifications';
	import NotificationSlot from '$lib/components/NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';
	import { DATA_KEPT, INSTALLER_ACTION, INSTALLER_URL } from '$lib/components/settings/pages/update-remedy';
	import {
		STOP_STOPS_IT, blockerView, notSent, offerDescribesStaged, operationOutcome, outcomeView, receiptRows, receiptText, refusedWorkers, waitingSummary, waitingView,
		type ActionResult, type Blocker, type BlockerView, type UpdateOperation, type WaitingFields
	} from '$lib/components/settings/pages/update-waiting';

	// Waiting fields (install_state, blocked_by...) come from update-waiting.ts; older peers omit them.
	type Worker = WaitingFields & { device_id: string; device_name: string; is_self?: boolean; phase: string; supported: boolean; online: boolean; allow_updates: boolean; version?: string | null; running_version?: string | null; percent?: number | null; message?: string | null; error?: string | null; reason?: string | null; release?: string | null; notes?: string | null; request_pending?: boolean; channel?: string | null; checked_at?: number | null; role?: Role; label?: string; deferred?: boolean; notice?: string | null; remedy?: 'installer' | null; data_kept?: boolean };
	/** The server's next step (app/core/update_workers.py plan()). Older servers omit it. */
	type Plan = { action?: 'install' | 'download' | 'check'; ready?: boolean; held?: string | null };
	type Snapshot = { allow_updates: boolean; update_all_workers: boolean; server_time?: number; workers: Worker[]; operation?: UpdateOperation | null; plan?: Plan | null };
	// excluded: not part of this update (offline, no updater, disabled, current, nothing offered).
	// unknown: an included worker whose state cannot be trusted; restart waits for it.
	type Role = 'excluded' | 'preparing' | 'available' | 'staged' | 'unknown';
	/** A chat still running on a worker that refused to restart (#1818). */
	type Busy = { session_id: string; title: string; tasks: string[] };
	type ReceiptRow = ActionResult & { busy?: Busy[]; deferred?: boolean; blocked_by?: Blocker[] | null; monitor_serving_idle_seconds?: number | null };
	/** `request_state: 'not_sent'`: a preflight refusal; nothing reached any worker. */
	/** `sent` is what the server actually sent; `held` says why it sent less than asked (a check instead of an install). */
	type Receipt = { request_state?: string | null; sent?: string | null; held?: string | null; results: ReceiptRow[] };
	type BusyGroup = { device_name: string; busy: Busy[]; blockers: BlockerView[] | null };
	/** The restart panel: work that kept a restart from running, what each worker's restart
	 *  waits for now (live from the snapshot), the workers a finished update left behind,
	 *  or workers only the latest installer can update. */
	type Panel = { kind: 'busy'; workers: BusyGroup[] } | { kind: 'waiting' } | { kind: 'outcome' } | { kind: 'installer' };
	/** States in which the restart is held or already requested. */
	const RESTART_WAITS = new Set(['blocked', 'waiting_for_idle', 'stopping', 'queued']);
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
	// The receipt only repeats the server's held text: it is history once the restart is ready.
	let receiptHeld = $state(false);
	let wasReady = false;
	let celebration = $state<'none' | 'complete' | 'leaving'>('none');
	// A finished update that left some workers behind, until the owner closes it.
	let partialShown = $state('');
	// An accepted stop / when-idle request, held until a snapshot read AFTER it
	// lands: the snapshot polled before still says "blocked" and must not
	// re-enable Stop (a second stop request). lockSeq is the action it belongs to.
	let optimistic = $state<'stopping' | 'waiting_for_idle' | 'queued' | null>(null);
	let actionSeq = 0;
	let lockSeq = 0;
	// Server clock minus this client's, from the snapshot's server_time.
	let skew = $state(0);
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
	// The aggregate outcome first, the legacy phase only for an older server (#344).
	const outcomeInfo = $derived(operation ? outcomeView(operation) : null);
	const waiting = $derived(outcomeInfo?.outcome === 'waiting' || installRequested);
	const celebrating = $derived(celebration !== 'none');
	// The worker classifies (app/core/update_workers.py classify()); the copy
	// below only covers a snapshot without roles.
	const offers = $derived(offeredUpdates(snapshot));
	const rows = $derived(snapshot?.workers.map(worker => withNotice(worker, worker.role && typeof worker.label === 'string'
		? { role: worker.role, label: worker.label } : classify(worker, snapshot!.update_all_workers, offers))) ?? []);
	// A Docker worker told ahead that a coming update needs a newer image (#1821).
	function withNotice(worker: Worker, row: { role: Role; label: string }) {
		// A just-accepted stop or schedule shows at once on the workers it held.
		const own = optimistic && worker.install_state === 'blocked' && row.role === 'staged'
			? { ...worker, install_state: optimistic, stop_result: null,
				wait_reason: optimistic === 'stopping' ? 'stopping_work' : optimistic === 'queued' ? 'updater_acknowledgement' : 'running_work' }
			: worker;
		// server_time is this worker's clock; a peer's epochs come from its own.
		const wait = waitingView(own, worker.is_self ? { skewSeconds: skew } : {});
		// The launcher's reason is technical; the installer panel shows it as detail.
		if (worker.remedy === 'installer') return { worker, role: row.role, label: 'Needs the latest installer', wait };
		// What a staged restart waits for says more than "Staged"; plain ready, and a
		// retained download the server already labels ("Check for updates before restarting"), keep the worker's label.
		const label = wait && wait.state !== 'ready' && wait.reason !== 'updater_idle' ? wait.title : row.label;
		return { worker, role: row.role, wait, label: worker.notice && worker.phase !== 'image-required' ? `${label}; a coming update needs a newer Docker image` : label };
	}
	const targets = $derived(rows.filter(row => row.role !== 'excluded'));
	// Restarts held by running work, scheduled for when it ends, being stopped, or requested.
	// Staged workers only: a newer offer elsewhere is never hidden behind the waiting panel.
	const restartWaits = $derived(targets.flatMap(row => row.role === 'staged' && row.wait && RESTART_WAITS.has(row.wait.state) ? [{ worker: row.worker, view: row.wait }] : []));
	const stopping = $derived(restartWaits.some(w => w.view.state === 'stopping'));
	const restartQueued = $derived(restartWaits.some(w => w.view.state === 'queued'));
	const blocked = $derived(restartWaits.some(w => w.view.state === 'blocked'));
	// A worker waiting to restart until its running work ends ("Restart when work finishes").
	const deferred = $derived(restartWaits.some(w => w.view.state === 'waiting_for_idle') || !!snapshot?.workers.some(w => w.deferred));
	// A stop or restart request is in flight: never send a second one.
	const restartLocked = $derived(stopping || restartQueued || !!optimistic);
	const named = $derived(restartWaits.length > 1 || !!snapshot?.update_all_workers);
	// A retained download on an idle updater: not installable until a check reads the feed again.
	// The server includes it (role available, "Check for updates before restarting"); an older
	// server that still excludes it shows no button for it, as before.
	const checkRows = $derived(targets.filter(row => row.wait?.reason === 'updater_idle'));
	const retainedIdle = $derived(checkRows.length > 0);
	const plan = $derived(snapshot?.plan ?? null);
	const downloading = $derived(targets.some(row => row.role === 'preparing'));
	// There is nothing to download for a retained download on an idle updater: its action is a check.
	const available = $derived(targets.some(row => row.role === 'available' && row.wait?.reason !== 'updater_idle'));
	const staged = $derived(targets.filter(row => row.role === 'staged').length);
	const unknown = $derived(targets.filter(row => row.role === 'unknown'));
	// Included workers whose update only the latest installer can finish
	// (app/core/update_workers.py remedy()). Clicking opens the download panel.
	const installer = $derived(targets.filter(row => row.worker.remedy === 'installer'));
	const dataKept = $derived(installer.length > 0 && installer.every(row => row.worker.data_kept));
	const installerMessage = $derived(installer.length
		? `${installer.length > 1 || snapshot?.update_all_workers ? `${installer.map(row => row.worker.device_name).join(', ')}: ` : ''}This update cannot finish inside the app. ${INSTALLER_ACTION} to finish it.${dataKept ? ` ${DATA_KEPT}` : ''}` : '');
	// Restart is offered only once EVERY included worker is staged, never on the first one.
	// The server's plan decides when it sends one; a retained download on an idle updater
	// anywhere in scope always holds the restart until a check reads the feed again (#361).
	const ready = $derived((typeof plan?.ready === 'boolean' ? plan.ready : staged > 0 && !downloading && !available && !unknown.length) && !retainedIdle);
	// The next click is a check, not an install: a staged worker waits for one that must check first.
	// The server holds the restart for a check (#361), even when the worker that must check is too
	// old to say so; unknown workers hold it for their own reason ("Restart waits for …").
	const checkHeld = $derived(retainedIdle || (staged > 0 && !unknown.length && plan?.action === 'check' && plan.ready === false));
	const checkFirst = $derived(!ready && !available && !downloading && checkHeld);
	const held = $derived(staged > 0 && !downloading && !available && unknown.length
		? `Restart waits for ${unknown.map(row => `${row.worker.device_name} (${row.label})`).join(', ')}.` : '');
	// Release notes travel in the signed manifest and describe the offered version.
	// A staged build that differs from that offer never wears its name or notes.
	const offered = $derived(targets.find(row => row.role !== 'unknown' && !!row.worker.notes?.trim()
		&& (row.role !== 'staged' || offerDescribesStaged(row.worker)))?.worker);
	// Refused: the restart never started on those workers, so never "startup could not be confirmed".
	const operationError = $derived(outcomeInfo?.outcome === 'refused' || outcomeInfo?.outcome === 'failed' ? outcomeInfo.detail : '');
	const partialNote = $derived(outcomeInfo?.outcome === 'partial' && operation && partialShown === operation.id ? outcomeInfo.detail : '');
	const error = $derived(requestError || operationError || installerMessage || targets.find(row => row.worker.error)?.worker.error || held);
	const visible = $derived(!!snapshot?.allow_updates && (waiting || celebrating || ready || available || downloading || restartWaits.length > 0 || checkFirst || !!partialNote || !!receipt || !!error));
	const busy = $derived(pending || downloading || waiting || celebrating || restartLocked);
	const action = $derived(ready ? 'Update and restart' : available ? 'Download update' : 'Check for updates');
	const scope = $derived(snapshot?.update_all_workers ? 'All online workers' : 'Current worker');
	const showOperation = $derived(!!operation && (waiting || celebrating || !!operationError || !!partialNote));
	const reports = $derived(showOperation
		? operation!.workers.map(w => `${w.device_name}: ${w.message}`).join('; ')
		: rows.map(row => `${row.worker.device_name}: ${row.label}`).join('; '));
	// A desktop's first start after an update can take minutes before its window returns; never look hung.
	const FIRST_START_NOTE = 'Starting the new version can take a few minutes.';
	const headline = $derived(celebrating ? 'Update complete' : waiting ? 'Waiting on all workers to finish startup'
		: stopping ? 'Stopping running work…' : restartQueued ? 'Restart requested' : deferred ? 'Restarts when work finishes'
		: operationError ? outcomeInfo!.headline : error ? 'Update needs attention' : blocked ? 'Ready; waiting for running work'
		: partialNote ? outcomeInfo!.headline : busy ? 'Preparing updates…' : action);
	// Name who must check first once another worker already waits staged for it.
	const checkWhy = $derived(staged > 0
		? `Restart waits for an update check${checkRows.length ? ` on ${checkRows.map(row => row.worker.device_name).join(', ')}` : ''}.`
		: 'Previous download retained; the updater is idle.');
	const checkText = $derived(`${checkWhy} Click to check for updates.`);
	const progressNote = $derived(targets.length > 1 ? ` ${staged} of ${targets.length} staged.` : '');
	const waitText = $derived(restartWaits.map(w => `${named ? `${w.worker.device_name}: ` : ''}${waitingSummary(w.view)}`).join(' '));
	const summary = $derived(error || waitText || (busy ? `Preparing updates…${progressNote}` : ready ? 'Update ready. Click to install and restart.' : partialNote
		|| (checkFirst ? checkText : 'Update available. Click to download.')));
	// Workers a still-running install will not restart, named while the rest finish.
	const refusedNote = $derived(waiting && outcomeInfo?.outcome === 'waiting' ? outcomeInfo.detail : '');
	const status = $derived(waiting ? `${headline}. ${FIRST_START_NOTE} ${reports}${refusedNote ? ` ${refusedNote}` : ''}` : celebrating ? `${headline}. ${reports}` : `${scope}: ${summary}${receipt ? ` Last request: ${receipt}.` : ''} ${reports}`);
	const panelLabel = $derived(blocked ? 'Work is still running' : stopping ? 'Stopping running work' : restartQueued ? 'Restart requested' : 'Restart scheduled');
	// The controls that fit any waiting worker; stop and restart stay visible but disabled while one is in flight.
	// A worker that must check first holds the restart: stop and restart would not be sent (#361).
	const offerStop = $derived(!checkHeld && (restartWaits.some(w => w.view.controls.stop) || restartLocked));
	const offerWhenIdle = $derived(!checkHeld && (restartWaits.some(w => w.view.controls.whenIdle) || (restartLocked && !deferred)));
	const offerCancel = $derived(restartWaits.some(w => w.view.controls.cancel));
	// Only the partial outcome keeps the button up: clicking shows who was left behind.
	const outcomeOnly = $derived(!!partialNote && !busy && !ready && !available && !error && !restartWaits.length);
	// The click shows what the restart waits for, so the button stays operable while it is busy.
	const opensPanel = $derived(restartWaits.length > 0 && !waiting && !celebrating);
	const leftBehind = $derived(operation && partialNote ? refusedWorkers(operation) : []);
	// A panel showing live restart state closes once nothing waits any more.
	$effect(() => { if (panel?.kind === 'waiting' && !restartWaits.length) panel = null; });
	$effect(() => { if (panel?.kind === 'outcome' && !partialNote) panel = null; });
	$effect(() => { canCheck = !!snapshot?.allow_updates; });
	// Clears on the turn to ready, not on a stale snapshot that already read ready when the server held it.
	$effect(() => {
		const now = ready;
		if (now && !wasReady && untrack(() => receiptHeld)) { receipt = ''; receiptHeld = false; }
		wasReady = now;
	});

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
		// Before the same-version test: the container updater reports the running revision here (#1901).
		if (w.phase === 'image-required') return { role: 'excluded', label: w.message || 'Needs a new Docker image; not included' };
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
	function openInstallerPage() { window.open(INSTALLER_URL, '_blank', 'noopener,noreferrer'); }
	function panelKey(event: KeyboardEvent) {
		if (panel && event.key === 'Escape') { event.preventDefault(); closePanel(); buttonEl?.focus(); }
	}
	/** Plain text of what is running, for the bell: "Fix login (Run tests); Update docs". */
	function busyText(workers: BusyGroup[]): string {
		return workers.map(w => `${workers.length > 1 ? `${w.device_name}: ` : ''}${w.blockers?.length
			? w.blockers.map(b => b.text.replace(/\.$/, '')).join('; ')
			: w.busy.map(b => b.tasks.length ? `${b.title} (${b.tasks.join(', ')})` : b.title).join('; ')}`).join('. ');
	}
	/** A restart refused because work is running: say so at once, here and in the bell (#1818). */
	function refusedForWork(result: Receipt): boolean {
		const workers = result.results.filter(w => w.accepted === false && !notSent(w) && (w.busy?.length || w.blocked_by?.length))
			.map(w => ({ device_name: w.device_name, busy: w.busy ?? [],
				blockers: w.blocked_by?.length ? w.blocked_by.map((b, i) => blockerView(b, i, w.monitor_serving_idle_seconds)) : null }));
		if (!workers.length) return false;
		openPanel({ kind: 'busy', workers });
		notify.warning(`Restart would stop running work: ${busyText(workers)}.`, {
			title: 'Restart is waiting', persist: false,
			action: { label: 'Restart when work finishes', onClick: () => void restartLater('when_idle') },
			secondaryAction: { label: 'Stop running work and restart', onClick: () => void restartLater('stop') }
		});
		return true;
	}
	/** Remember a request's refusals: not-sent rows are not the worker's refusal, so they only show as the receipt. */
	function keepReceipt(result: Receipt) {
		receipt = receiptText(receiptRows(result.results));
		receiptHeld = false;
		refusals = Object.fromEntries(result.results.filter(w => !w.accepted && !notSent(w) && w.device_id).map(w => [w.device_id!, w.reason]));
	}
	/** "Restart when work finishes" or "Stop running work and restart". */
	async function restartLater(mode: 'when_idle' | 'stop') {
		closePanel();
		if (pending || restartLocked) return;
		pending = true; requestError = '';
		try {
			const result = await api.post<Receipt>('/settings/application-updates', { action: 'install', mode });
			if (!active) return;
			const refused = result.results.filter(w => !w.accepted);
			keepReceipt(result);
			const accepted = result.results.filter(w => w.accepted);
			if (accepted.length) {
				const said = accepted.map(w => w.install_state).find(state => state === 'stopping' || state === 'waiting_for_idle' || state === 'queued');
				optimistic = (said as typeof optimistic) ?? (mode === 'stop' ? 'stopping' : 'waiting_for_idle');
				lockSeq = ++actionSeq;
			}
			if (refused.length) notify.warning(receipt, { title: refused.every(notSent) ? 'Restart was not sent' : 'Update needs attention', persist: false });
			else notify.info(mode === 'stop' ? 'Stop requested. Stopping running work, then requesting restart…' : 'Restarts when running work finishes.',
				{ title: mode === 'stop' ? 'Stopping running work' : 'Restart scheduled', persist: false });
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
			if (active) notify.info('The update stays downloaded.', { title: 'Schedule cancelled', persist: false });
		} catch (cause) {
			if (active) notify.error(getErrorMessage(cause, 'Could not cancel the restart'));
		} finally { if (active) { pending = false; optimistic = null; void refresh(); } }
	}
	function closeOutcome() { partialShown = ''; closePanel(); }
	function clicked() {
		if (panel) { closePanel(); return; }
		// What the restart waits for, and the controls that fit, rather than a request that would be refused.
		if (opensPanel) { openPanel({ kind: 'waiting' }); void refresh(); return; }
		if (outcomeOnly) { openPanel({ kind: 'outcome' }); return; }
		// A retained download the updater cannot install yet: check, and always say what was found.
		// Even beside a worker error: the check always reports what it found.
		if (checkFirst && !busy && !installer.length) { void checkForUpdates(); return; }
		if (installer.length && !busy) { openPanel({ kind: 'installer' }); return; }
		void activate();
	}
	function applySnapshot(next: Snapshot) {
		snapshot = next;
		skew = typeof next.server_time === 'number' ? next.server_time - Date.now() / 1000 : 0;
		if (!pending) installRequested = false;
		const done = next.operation;
		if (done && operationOutcome(done) === 'waiting') watched = done.id;
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
		const view = outcomeView(done);
		// Workers were left behind: name them once, never an all-worker success.
		if (!view.celebrate) {
			if (recent && view.outcome === 'partial') {
				partialShown = done.id;
				notify.warning(view.detail, { title: view.headline, persist: false });
			}
			return;
		}
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
		const seq = actionSeq;
		try {
			const next = await api.get<Snapshot>('/settings/application-updates/workers');
			if (!active) return;
			// Read after the accepted action: the worker's own state now speaks for it.
			if (optimistic && seq >= lockSeq) optimistic = null;
			applySnapshot(next);
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
		// Retrying in the app would fail the same way.
		if (installer.length) { openPanel({ kind: 'installer' }); return; }
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
		else if (error) notify.warning(error, { title: 'Update check needs attention', persist: false,
			...(installer.length ? { action: { label: INSTALLER_ACTION, onClick: openInstallerPage } } : {}) });
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
			keepReceipt(result);
			if (!result.results.some(w => w.accepted)) installRequested = false;
			// The server sent less than asked (a check instead of an install): nothing restarts,
			// so never wait on startup, and say why whether or not this request was announced.
			const sentAction = typeof result.sent === 'string' ? result.sent : requestedAction;
			if (sentAction !== requestedAction || result.held) {
				installRequested = false;
				const why = result.held?.replace(/\.+$/, '') || 'Nothing was installed';
				receipt = [why, receipt].filter(Boolean).join('; ');
				receiptHeld = true;
				if (sentAction === 'check' && result.results.some(w => w.accepted)) manualCheckAt = Date.now();
				notify.warning(`${why}.${sentAction === 'check' ? ' Checking for updates first.' : sentAction === 'download' ? ' Downloading first.' : ''}`,
					{ title: 'Restart held', persist: false });
				return result;
			}
			if (requestedAction === 'install' && refusedForWork(result)) return result;
			if (announce) {
				const sent = result.results.filter(w => !notSent(w)).map(w => `${w.device_name}: ${w.reason}`);
				const each = [receiptText(result.results.filter(notSent)), ...sent].filter(Boolean).join('; ');
				const message = `${scope}. ${each}${requestedAction === 'download' ? ' When preparation finishes, choose Update and restart to install.' : ''}`;
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

{#snippet blockerList(blockers: BlockerView[], stopOffered = true)}
	<ul class="busy-list blockers">
		{#each blockers as blocker (blocker.key)}
			<li data-kind={blocker.kind}>
				<span class="kind-tag">{blocker.kind}</span>
				<span class="busy-title">{blocker.text}</span>
				{#each blocker.meta.filter(line => stopOffered || line !== STOP_STOPS_IT) as line (line)}<span class="busy-tasks">{line}</span>{/each}
			</li>
		{/each}
	</ul>
{/snippet}

{#if visible}
	<div class="update-slot" class:complete={celebrating} class:leaving={celebration === 'leaving'}>
		<button class="update-button" class:ready class:failed={!!error && !waiting && !celebrating && !deferred}
			class:held={(blocked || deferred) && !error && !waiting && !celebrating} class:attention={outcomeOnly}
			type="button" onclick={clicked} aria-disabled={busy && !opensPanel} aria-busy={busy && !celebrating} bind:this={buttonEl}
			aria-label={waiting || celebrating || deferred || restartLocked || outcomeOnly || (blocked && !error) ? headline : busy ? 'Downloading update' : installer.length ? 'Update needs the latest installer' : action}
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
			{:else if error || outcomeOnly}<CircleAlert size={16} strokeWidth={1.85} />
			{:else if ready}<RefreshCw size={16} strokeWidth={1.85} />
			{:else}<Download size={16} strokeWidth={1.85} />{/if}
		</button>
		{#if panel}
			<NotificationSlot order={DOCK_ORDER.restart}>
			<div class="update-panel" id={panelId} role="dialog" aria-label={panel.kind === 'busy' ? 'Work is still running' : panel.kind === 'installer' ? 'Update needs the latest installer' : panel.kind === 'outcome' ? (outcomeInfo?.headline ?? 'Update finished') : panelLabel}>
				{#if panel.kind === 'installer'}
					<strong>Update needs the latest installer</strong>
					<p class="panel-hint">This update cannot finish inside the app. Run the latest installer to finish it.{dataKept ? ` ${DATA_KEPT}` : ''}</p>
					{#each installer as row (row.worker.device_id)}
						{#if row.worker.error || row.worker.message}
							<span class="panel-worker">{installer.length > 1 || snapshot?.update_all_workers ? `${row.worker.device_name}: ` : ''}{row.worker.error || row.worker.message}</span>
						{/if}
					{/each}
					<div class="panel-actions">
						<a class="panel-primary" href={INSTALLER_URL} target="_blank" rel="noopener noreferrer" onclick={closePanel}>{INSTALLER_ACTION}</a>
						<button type="button" class="panel-quiet" onclick={closePanel}>Not now</button>
					</div>
				{:else if panel.kind === 'busy'}
					<strong>Work is still running</strong>
					<p class="panel-hint">Restart would stop it.</p>
					{#each panel.workers as group (group.device_name)}
						{#if panel.workers.length > 1}<span class="panel-worker">{group.device_name}</span>{/if}
						{#if group.blockers}
							{@render blockerList(group.blockers)}
						{:else}
							<ul class="busy-list">
								{#each group.busy as item (item.session_id)}
									<li><span class="busy-title">{item.title}</span>{#if item.tasks.length}<span class="busy-tasks">{item.tasks.join(', ')}</span>{/if}</li>
								{/each}
							</ul>
						{/if}
					{/each}
					<div class="panel-actions">
						<button type="button" class="panel-primary" onclick={() => restartLater('when_idle')}>Restart when work finishes</button>
						<button type="button" class="panel-danger" onclick={() => restartLater('stop')}>Stop running work and restart</button>
						<button type="button" class="panel-quiet" onclick={closePanel}>Not now</button>
					</div>
				{:else if panel.kind === 'outcome'}
					<strong>{outcomeInfo?.headline}</strong>
					<p class="panel-hint">Not restarted:</p>
					<ul class="busy-list">
						{#each leftBehind as worker, i (worker.device_id ?? i)}
							<li><span class="busy-title">{worker.device_name}</span><span class="busy-tasks">{worker.reason}</span></li>
						{/each}
					</ul>
					<div class="panel-actions">
						<button type="button" class="panel-quiet" onclick={closeOutcome}>Close</button>
					</div>
				{:else}
					<strong>{panelLabel}</strong>
					{#each restartWaits as wait (wait.worker.device_id)}
						<section class="wait-group" data-state={wait.view.state}>
							{#if named}<span class="panel-worker">{wait.worker.device_name}</span>{/if}
							<p class="wait-text"><i class="tone-dot {wait.view.tone}" aria-hidden="true"></i><span>{wait.view.text}</span></p>
							{#if wait.view.blockers?.length}{@render blockerList(wait.view.blockers, offerStop)}{/if}
							{#each wait.view.notes as note (note)}<p class="wait-note">{note}</p>{/each}
						</section>
					{/each}
					{#if restartLocked}<p class="wait-note">Another stop or restart can be requested once this one finishes.</p>{/if}
					{#if checkHeld && !restartLocked}<p class="wait-text check-first"><i class="tone-dot warn" aria-hidden="true"></i><span>{checkWhy} Restart controls return once every worker is staged.</span></p>{/if}
					<div class="panel-actions">
						{#if checkHeld && !restartLocked}<button type="button" class="panel-primary" disabled={pending} onclick={() => { closePanel(); void checkForUpdates(); }}>Check for updates</button>{/if}
						{#if offerWhenIdle}<button type="button" class="panel-primary" disabled={pending || restartLocked} onclick={() => restartLater('when_idle')}>Restart when work finishes</button>{/if}
						{#if offerStop}<button type="button" class="panel-danger" disabled={pending || restartLocked} onclick={() => restartLater('stop')}>Stop running work and restart</button>{/if}
						{#if offerCancel}<button type="button" class="panel-outline" disabled={pending} onclick={cancelRestart}>Cancel schedule</button>{/if}
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
					{#if operationError || partialNote}<span class="tip-lead">{operationError || partialNote}</span>{/if}
					{#each operation.workers as worker (worker.device_id)}
						<div class="worker-row"><span>{worker.device_name}</span><span class:verified={worker.status === 'verified'} class:skipped={worker.status === 'skipped'}>{worker.status === 'skipped' ? `Not restarted: ${worker.message}` : worker.message}</span></div>
					{/each}
					{#if refusedNote}<p>{refusedNote}</p>{/if}
				{:else}
					{#if error || !restartWaits.length}<span>{scope}: {summary}</span>{/if}
					{#each rows as row (row.worker.device_id)}
						<div class="worker-row"><span>{row.worker.device_name}</span><span class:verified={row.role === 'staged' && (!row.wait || row.wait.state === 'ready')}>{row.label}</span></div>
						{#if row.wait && row.wait.state !== 'ready' && row.role !== 'excluded' && row.wait.reason !== 'updater_idle'}
							<div class="wait-tip">
								<span>{row.wait.text}</span>
								{#each (row.wait.blockers ?? []).slice(0, 3) as blocker (blocker.key)}<span class="wait-tip-item">{blocker.text}</span>{/each}
								{#if (row.wait.blockers?.length ?? 0) > 3}<span class="wait-tip-item">and {row.wait.blockers!.length - 3} more</span>{/if}
								{#each row.wait.notes as note (note)}<span class="wait-tip-item">{note}</span>{/each}
							</div>
						{/if}
					{/each}
					{#if receipt}<p>Last request: {receipt}</p>{/if}
				{/if}
				{#if offered && !waiting && !celebrating}
					<div class="release-notes">
						<strong>What's new{offered.release ? ` in ${offered.release}` : ''}</strong>
						<div class="notes-text">{offered.notes}</div>
					</div>
				{/if}
				{#if waiting}<p>Checking that every worker included in this update has the new version. {FIRST_START_NOTE}</p>{/if}
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
	.update-tooltip { position: fixed; z-index: 250; width: 280px; max-width: calc(100vw - 16px); padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--popover); color: var(--popover-foreground); box-shadow: 0 8px 24px #0004; font-size: 11px; line-height: 1.5; pointer-events: none; overflow-wrap: break-word; }
	.update-tooltip strong { display: block; font-size: 12px; margin-bottom: 8px; }
	.worker-row { display: flex; justify-content: space-between; gap: 12px; margin-top: 6px; }
	.worker-row span:last-child { text-align: right; color: var(--muted-foreground); }
	.worker-row .verified { color: var(--primary) !important; }
	.release-notes { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); }
	.release-notes strong { margin-bottom: 4px; }
	.notes-text { white-space: pre-line; color: var(--muted-foreground); display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 12; line-clamp: 12; overflow: hidden; }
	.update-tooltip p { margin: 10px 0 0; padding-top: 8px; border-top: 1px solid var(--border); color: var(--muted-foreground); }
	.update-panel { width: 100%; max-width: 100%; padding: 12px 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--card); color: var(--card-foreground, var(--foreground)); box-shadow: var(--shadow-l); font-size: 13px; line-height: 1.45; overflow-wrap: break-word; }
	.update-panel strong { display: block; font-size: 14px; }
	.panel-hint { margin: 2px 0 8px; color: var(--muted-foreground); }
	.panel-worker { display: block; margin-top: 6px; font-size: 11px; color: var(--muted-foreground); }
	.busy-list { margin: 4px 0 0; padding: 0; list-style: none; max-height: 160px; overflow-y: auto; }
	.busy-list li { display: flex; flex-direction: column; padding: 5px 8px; border-radius: 6px; background: color-mix(in oklch, var(--muted) 60%, transparent); }
	.busy-list li + li { margin-top: 4px; }
	.busy-title { color: var(--foreground); }
	.busy-tasks { font-size: 11px; color: var(--muted-foreground); }
	.panel-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
	.panel-actions button, .panel-actions a { min-height: 32px; padding: 0 10px; border-radius: 7px; border: 1px solid transparent; font-size: 12px; text-align: center; cursor: pointer; }
	.panel-actions a { display: inline-flex; align-items: center; text-decoration: none; }
	.panel-actions button:focus-visible, .panel-actions a:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.panel-primary { color: var(--primary-foreground); background: var(--primary); }
	.panel-primary:hover { background: color-mix(in oklch, var(--primary) 88%, var(--foreground)); }
	.panel-actions .panel-danger { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 10%, transparent); border-color: color-mix(in oklch, var(--destructive) 30%, transparent); }
	.panel-danger:hover { background: color-mix(in oklch, var(--destructive) 18%, transparent); }
	.panel-quiet { color: var(--muted-foreground); background: transparent; }
	.panel-actions .panel-outline { color: var(--foreground); background: transparent; border-color: var(--border); }
	.panel-outline:hover { background: var(--hover-overlay, color-mix(in oklch, var(--muted) 60%, transparent)); }
	.panel-actions button:disabled { opacity: .45; cursor: not-allowed; }
	/* Restart waiting state (#344): one group per worker, a tone dot, then what still runs. */
	.wait-group { margin-top: 8px; }
	.wait-group + .wait-group { padding-top: 8px; border-top: 1px dashed var(--border); }
	.wait-text { display: flex; gap: 8px; align-items: baseline; margin: 4px 0 0; }
	.wait-text.check-first { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); }
	.tone-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; transform: translateY(-1px); background: var(--muted-foreground); }
	.tone-dot.warn { background: var(--warning); }
	.tone-dot.err { background: var(--destructive); }
	.tone-dot.ok { background: var(--success); }
	.tone-dot.busy { background: var(--primary); animation: wait-pulse 1.4s ease-in-out infinite; }
	.wait-note { margin: 6px 0 0; font-size: 11px; color: var(--muted-foreground); }
	.blockers { margin-top: 8px; max-height: 220px; }
	.blockers li { gap: 2px; padding: 6px 8px 6px 10px; border-left: 2px solid color-mix(in oklch, var(--muted-foreground) 45%, transparent); border-radius: 2px 6px 6px 2px; }
	.blockers li[data-kind='service'] { border-left-color: var(--warning); }
	.blockers li[data-kind='turn'] { border-left-color: var(--primary); }
	.kind-tag { align-self: flex-start; font-family: var(--font-mono); font-size: 9.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted-foreground); }
	.update-button.attention { color: var(--warning); background: color-mix(in oklch, var(--warning) 14%, transparent); }
	.update-button.held::after { content: ''; position: absolute; top: 3px; right: 3px; width: 6px; height: 6px; border-radius: 50%; background: var(--warning); box-shadow: 0 0 0 2px var(--sidebar, var(--background)); }
	.tip-lead { display: block; margin-bottom: 4px; }
	.worker-row .skipped { color: var(--warning) !important; }
	.wait-tip { display: flex; flex-direction: column; gap: 2px; margin: 3px 0 2px; padding-left: 8px; border-left: 2px solid var(--border); color: var(--muted-foreground); }
	.wait-tip-item { font-size: 10.5px; }
	@keyframes wait-pulse { 50% { opacity: .35; } }
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
	@media (max-width: 639px), (pointer: coarse) { .panel-actions button, .panel-actions a { min-height: 44px; } }
	@media (hover: none) { .update-slot { width: 40px; height: 40px; } .update-button { border-radius: 11px; } }
	@media (prefers-reduced-motion: reduce) {
		.update-button :global(.update-spinner), .completion-ring, .complete .update-button, .leaving { animation: none; }
		.complete .completion-ring { animation: none; opacity: 0; }
		.complete .completion-check { animation: none; opacity: 1; stroke-dashoffset: 0; }
		.spark, .halo { display: none; }
		.tone-dot.busy { animation: none; }
		.leaving .update-button { animation: none; opacity: 0; }
	}
</style>
