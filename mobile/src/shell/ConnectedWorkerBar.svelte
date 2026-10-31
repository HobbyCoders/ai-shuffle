<script lang="ts">
	/**
	 * "Connected to <worker>" (issue #1816): the worker chip on the second line
	 * of the phone's one bar (AppTopBar). It names the worker this phone is
	 * connected to and opens a sheet listing the owner's workers (online state,
	 * running chats, the current one marked) to connect the app to another.
	 * This is never where a chat runs: moving one chat is "Runs on" in Chat
	 * controls, and the sheet says so.
	 *
	 * In the Android app this chip replaces the app's own worker-name bar: it
	 * tells the app it is shown (native-shell.ts). Worker management lives in
	 * Settings > Devices; the native Workers screen is a connection-recovery
	 * fallback, so pairing remains available without a reachable worker. In the
	 * app the sheet also opens the app's own Settings (account, sign out, phone
	 * tools), which the hidden native bar no longer reaches (#951).
	 */
	import { onMount, tick } from 'svelte';
	import { obscuresTranscript } from '$lib/components/chat/transcript-reading-area';
	import { Check, Info, LoaderCircle, Server, Settings, Smartphone, X } from 'lucide-svelte';
	import { listInstalls, type Install } from '$lib/api/installs';
	import { isBundledMobile, nativeCapability } from '$lib/api/runtime';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { streamingSessions } from '$lib/stores/active-session';
	import { notify } from '$lib/stores/notifications';
	import { openSettings } from '$lib/settings/navigation.svelte';
	import { connectionWorkers, switchRoute, WORKER_SWITCH_CAPABILITY, type ConnectionWorker } from './worker-connection';
	import { announceWorkerBar, hasShellBridge, nativeAppSettingsRoute, openNativeAppSettings, openNativeWorkers } from './native-shell';

	/** How long "Connecting to" waits before giving the bar back (the app refused, or nothing happened). */
	const CONNECT_TIMEOUT_MS = 5000;
	const STATUS_REFRESH_MS = 15000;
	const STATUS_TIMEOUT_MS = 5000;

	let installs = $state<Install[]>([]);
	let loaded = $state(false);
	let loading = $state(false);
	let loadError = $state(false);
	let browserOnline = $state(true);
	let open = $state(false);
	let connecting = $state<string | null>(null);
	let barRef = $state<HTMLButtonElement | null>(null);
	let sheetRef = $state<HTMLElement | null>(null);
	let mounted = false;
	let connectTimer: ReturnType<typeof setTimeout> | undefined;
	let statusTimer: ReturnType<typeof setTimeout> | undefined;
	let statusRequest: AbortController | null = null;

	async function load() {
		if (loading || !mounted) return;
		loading = true;
		const request = new AbortController();
		statusRequest = request;
		statusTimer = setTimeout(() => request.abort(), STATUS_TIMEOUT_MS);
		try {
			const result = await listInstalls({ signal: request.signal });
			if (!mounted) return;
			installs = result.installs;
			loadError = false;
		} catch {
			if (mounted) loadError = true;
		} finally {
			clearTimeout(statusTimer);
			statusRequest = null;
			if (mounted) {
				loaded = true;
				loading = false;
			}
		}
	}

	onMount(() => {
		mounted = true;
		browserOnline = navigator.onLine;
		void load();
		const refresh = () => {
			if (document.visibilityState === 'visible' && browserOnline) void load();
		};
		const online = () => { browserOnline = true; refresh(); };
		const offline = () => { browserOnline = false; statusRequest?.abort(); };
		const timer = setInterval(refresh, STATUS_REFRESH_MS);
		window.addEventListener('online', online);
		window.addEventListener('offline', offline);
		document.addEventListener('visibilitychange', refresh);
		return () => {
			mounted = false;
			clearInterval(timer);
			clearTimeout(connectTimer);
			clearTimeout(statusTimer);
			statusRequest?.abort();
			window.removeEventListener('online', online);
			window.removeEventListener('offline', offline);
			document.removeEventListener('visibilitychange', refresh);
		};
	});

	// The app hides its own worker-name bar while this one is on screen.
	const canManage = hasShellBridge();
	// With that bar hidden, this sheet is the way into the app's own Settings
	// (account, sign out, phone tools). An app from before the `app-settings`
	// message reaches them through its Workers screen.
	const appSettingsRoute = nativeAppSettingsRoute();
	$effect(() => {
		announceWorkerBar(true);
		return () => { announceWorkerBar(false); };
	});

	const streaming = $derived(new Set([...$streamingSessions.keys()]));
	// Only this worker's fresh request result is evidence of the phone's connection.
	const currentReachable = $derived(!browserOnline || loadError ? false : loaded ? true : undefined);
	const workers = $derived(connectionWorkers(installs, $sessions, streaming, currentReachable));
	const current = $derived(workers.find((w) => w.current) ?? null);
	const currentName = $derived(current?.name ?? (loaded ? 'this worker' : '…'));
	const currentOnline = $derived(currentReachable === true && current?.online === true);
	const nativeManagement = $derived(canManage && (loadError || !browserOnline));
	const connectionLabel = $derived(
		!browserOnline || loadError ? `Connection to ${currentName} lost. Switch worker`
			: !loaded ? 'Checking worker connection. Switch worker'
			: !current ? 'Worker connection unknown. Switch worker'
			: currentOnline ? `Connected to ${currentName}. Switch worker`
			: `${currentName} is offline. Switch worker`
	);

	async function openSheet() {
		open = true;
		void load();
		await tick();
		sheetRef?.querySelector<HTMLElement>('[aria-checked="true"], button')?.focus({ preventScroll: true });
	}

	function closeSheet() {
		open = false;
		barRef?.focus({ preventScroll: true });
	}

	function connect(worker: ConnectionWorker) {
		const route = switchRoute(worker, {
			inApp: isBundledMobile(),
			canSwitch: nativeCapability(WORKER_SWITCH_CAPABILITY),
			pageUrl: window.location.href
		});
		if (route.kind === 'current') { closeSheet(); return; }
		if (route.kind === 'unavailable') { notify(route.reason, { severity: 'info' }); return; }
		connecting = worker.id;
		window.location.assign(route.url);
		// The app answers a refused switch natively and stays here; give the bar back.
		clearTimeout(connectTimer);
		connectTimer = setTimeout(() => { if (connecting === worker.id) connecting = null; }, CONNECT_TIMEOUT_MS);
	}

	function manageWorkers() {
		closeSheet();
		if (nativeManagement && openNativeWorkers()) return;
		openSettings({ category: 'devices' });
	}

	function appSettings() {
		closeSheet();
		openNativeAppSettings();
	}

	function keydown(event: KeyboardEvent) {
		if (!open || event.defaultPrevented) return;
		if (event.key === 'Escape') {
			event.preventDefault();
			closeSheet();
		} else if (event.key === 'Tab' && sheetRef) {
			const buttons = [...sheetRef.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
			const first = buttons[0];
			const last = buttons.at(-1);
			if (event.shiftKey && document.activeElement === first) {
				event.preventDefault();
				last?.focus();
			} else if (!event.shiftKey && document.activeElement === last) {
				event.preventDefault();
				first?.focus();
			}
		}
	}
</script>

<svelte:window onkeydown={keydown} />

<button
	bind:this={barRef}
	type="button"
	class="conn-chip"
	class:busy={!!connecting}
	aria-haspopup="dialog"
	aria-expanded={open}
	aria-label={connecting ? `Connecting to ${workers.find((w) => w.id === connecting)?.name ?? 'worker'}` : connectionLabel}
	title={connectionLabel.replace('. Switch worker', '')}
	onclick={openSheet}
>
	{#if connecting}
		<LoaderCircle size={11} class="conn-spin" aria-hidden="true" />
		<b>{workers.find((w) => w.id === connecting)?.name ?? 'worker'}</b>
	{:else}
		{#if !loaded && browserOnline}
			<LoaderCircle size={11} class="conn-spin" aria-hidden="true" />
		{:else}
			<span class="dot" class:off={!currentOnline} aria-hidden="true"></span>
		{/if}
		<b>{currentName}</b>
	{/if}
</button>

{#if open}
	<div class="conn-portal" use:portal>
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="scrim" onclick={closeSheet}></div>
		<div bind:this={sheetRef} use:obscuresTranscript class="sheet" role="dialog" aria-modal="true" aria-labelledby="conn-title" aria-describedby="conn-description" data-no-drawer-swipe>
			<div class="sheet-h">
				<h4 id="conn-title">Connected to</h4>
				<button type="button" class="icon-btn" aria-label="Close" onclick={closeSheet}><X size={18} aria-hidden="true" /></button>
			</div>
			<p id="conn-description" class="sub">The worker this phone connects through. Pick one to switch now.</p>
			{#if !loaded}
				<p class="empty" role="status"><LoaderCircle size={16} class="conn-spin" aria-hidden="true" />Loading workers</p>
			{:else}
				{#if loadError || !browserOnline}
					<div class="load-error">
						<p role="alert">Could not refresh worker status.{workers.length ? ' Showing the last known workers.' : ''}</p>
						<button type="button" class="retry" disabled={loading} onclick={() => { void load(); }}>{loading ? 'Checking…' : 'Try again'}</button>
					</div>
				{/if}
				{#if workers.length}
					<div class="opt-list" role="radiogroup" aria-label="Connected to">
						{#each workers as worker (worker.id)}
							<button type="button" class="opt-row" role="radio" aria-checked={worker.current} disabled={!!connecting} onclick={() => connect(worker)}>
								<span class="wk-ic"><Server size={18} aria-hidden="true" /></span>
								<span class="wk">
									<b>{worker.name}</b>
									<small><span class="dot" class:off={loadError || !browserOnline || !worker.online} aria-hidden="true"></span><span>{loadError || !browserOnline ? 'Status unavailable' : worker.online ? 'Online' : 'Offline'} · {worker.running} running chat{worker.running === 1 ? '' : 's'}{worker.current ? ' · Current' : ''}</span></small>
								</span>
								<span class="ck">{#if connecting === worker.id}<LoaderCircle size={18} class="conn-spin" aria-hidden="true" />{:else if worker.current}<Check size={18} aria-hidden="true" />{/if}</span>
							</button>
						{/each}
					</div>
				{:else if !loadError}
					<p class="empty">No workers found. Add a worker in Settings > Devices.</p>
				{/if}
			{/if}
			<div class="actions">
				<button type="button" class="manage" aria-label={nativeManagement ? 'Open app Workers' : 'Manage workers'} disabled={!!connecting} onclick={manageWorkers}>
					<Settings size={16} aria-hidden="true" />
					<span>{nativeManagement ? 'Open app Workers' : 'Manage workers'}</span>
					{#if !nativeManagement}<small>Settings > Devices</small>{/if}
				</button>
				{#if appSettingsRoute}
					<button type="button" class="manage" aria-label="App settings" aria-describedby="conn-app-settings" disabled={!!connecting} onclick={appSettings}>
						<Smartphone size={16} aria-hidden="true" />
						<span>App settings</span>
						<small id="conn-app-settings">{appSettingsRoute === 'settings' ? 'Account, sign out' : 'Workers > Settings'}</small>
					</button>
				{/if}
			</div>
			<p class="note"><Info size={15} aria-hidden="true" />To move one chat, use Runs on in Chat controls.</p>
		</div>
	</div>
{/if}

<script module lang="ts">
	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return { destroy() { node.remove(); } };
	}
</script>

<style>
	/* A 44px-tall tap target whose dot + name ride on the bar's second line
	   (text at the bottom, 7px above the bar's edge). */
	.conn-chip {
		/* Keeps its own width (up to 40% of the line); the chat trail after it
		   gives way first, so the worker name stays readable. */
		flex: 0 0 auto;
		min-width: 44px;
		max-width: 40%;
		/* 44px tall (a full tap target); the text rides on its bottom edge, so
		   the top 28px reach up beside the title line without moving it. */
		height: 44px;
		display: flex;
		align-items: flex-end;
		gap: 5px;
		padding: 0 2px 7px 0;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 11.5px;
		line-height: 16px;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
		transition: color 120ms ease;
	}
	.conn-chip:hover, .conn-chip[aria-expanded='true'] { color: var(--foreground); }
	.conn-chip:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	/* Centred on the 16px text line. */
	.conn-chip .dot { margin-bottom: 5px; }
	.conn-chip :global(.conn-spin) { flex: none; margin-bottom: 2.5px; color: var(--warning); }
	.conn-chip.busy b { color: var(--warning); }
	.conn-chip b { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
	.dot { display: inline-block; flex-shrink: 0; width: 6px; height: 6px; border-radius: 50%; background: var(--success, var(--primary)); }
	/* A hollow dot distinguishes unknown/offline without relying on color. */
	.dot.off { background: transparent; box-shadow: inset 0 0 0 1.5px var(--muted-foreground); }

	.scrim { position: fixed; inset: 0; z-index: 140; background: var(--modal-overlay, color-mix(in oklch, var(--background) 60%, transparent)); animation: conn-fade 0.2s ease; }
	.sheet {
		position: fixed;
		/* Full width on a phone; a centred 560px card in a wide DeX window. */
		left: max(8px, calc(50% - 280px));
		right: max(8px, calc(50% - 280px));
		bottom: calc(12px + env(safe-area-inset-bottom, 0px));
		z-index: 141;
		max-height: calc(var(--app-viewport-height, 100dvh) - 96px);
		overflow-y: auto;
		overscroll-behavior: contain;
		padding: 16px 16px 10px;
		border: 1px solid var(--border-strong);
		border-radius: calc(var(--radius) * 2);
		background: var(--popover);
		color: var(--foreground);
		box-shadow: var(--shadow-l);
		animation: conn-up 0.3s cubic-bezier(0.22, 1, 0.36, 1);
	}
	@keyframes conn-fade { from { opacity: 0; } }
	@keyframes conn-up { from { transform: translateY(calc(100% + 40px)); } }
	.sheet-h { display: flex; align-items: center; justify-content: space-between; }
	.sheet-h h4 { margin: 0; font-size: 16px; font-weight: 600; }
	.icon-btn { display: grid; place-items: center; width: 44px; height: 44px; margin-right: -8px; border: 0; border-radius: var(--radius); background: transparent; color: var(--muted-foreground); cursor: pointer; }
	.icon-btn:hover { background: var(--hover-overlay); color: var(--foreground); }
	.sub { margin: 0 0 10px; font-size: 13px; color: var(--muted-foreground); }
	.opt-list { display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--card); }
	.opt-row {
		min-height: 60px;
		display: grid;
		grid-template-columns: 34px minmax(0, 1fr) 22px;
		align-items: center;
		gap: 12px;
		padding: 8px 14px;
		border: 0;
		border-top: 1px solid var(--border-subtle);
		background: transparent;
		color: var(--foreground);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.opt-row:first-child { border-top: 0; }
	.opt-row:hover:not(:disabled) { background: var(--hover-overlay); }
	.opt-row:disabled, .manage:disabled, .retry:disabled { cursor: progress; opacity: 0.6; }
	.opt-row, .manage, .retry, .icon-btn { transition: background 0.2s ease, opacity 0.2s ease; }
	.opt-row:active:not(:disabled), .manage:active:not(:disabled), .retry:active:not(:disabled), .icon-btn:active { background: var(--active-overlay); }
	.opt-row[aria-checked='true'] b { color: var(--primary); }
	.wk-ic { display: grid; place-items: center; width: 34px; height: 34px; border-radius: calc(var(--radius) - 2px); background: var(--muted); color: var(--muted-foreground); }
	.wk { min-width: 0; }
	.wk b { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 550; }
	.wk small { display: flex; align-items: center; gap: 6px; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); }
	.ck { color: var(--primary); }
	.empty { display: flex; align-items: center; gap: 8px; padding: 14px 4px; font-size: 13px; color: var(--muted-foreground); }
	.retry { min-height: 44px; padding: 0 14px; border: 1px solid var(--border-strong); border-radius: var(--radius); background: var(--card); color: var(--foreground); font: inherit; cursor: pointer; }
	.load-error { margin-bottom: 12px; }
	.load-error p { margin: 0 0 8px; font-size: 13px; color: var(--muted-foreground); }
	/* Manage workers and, in the app, App settings: one card of rows, like the worker list. */
	.actions { display: flex; flex-direction: column; margin-top: 10px; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--card); }
	.manage { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; width: 100%; min-height: 44px; padding: 10px 12px; border: 0; background: transparent; color: var(--foreground); font: inherit; font-size: 13px; text-align: left; cursor: pointer; }
	.manage + .manage { border-top: 1px solid var(--border-subtle); }
	.manage:hover:not(:disabled), .retry:hover:not(:disabled) { background: var(--hover-overlay); }
	.manage :global(svg) { flex: none; color: var(--muted-foreground); }
	.manage small { margin-left: auto; font-size: 11px; color: var(--muted-foreground); }
	.note { display: flex; align-items: flex-start; gap: 8px; margin: 12px 2px 2px; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); }
	.note :global(svg) { flex: none; margin-top: 2px; }
	.opt-row:focus-visible, .icon-btn:focus-visible, .retry:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	/* Inside the clipped card, so the ring sits inside the row. */
	.manage:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	:global(.conn-spin) { animation: conn-spin 0.9s linear infinite; }
	@keyframes conn-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.scrim, .sheet, :global(.conn-spin) { animation: none; }
		.conn-chip, .opt-row, .manage, .retry, .icon-btn { transition: none; }
	}
</style>
