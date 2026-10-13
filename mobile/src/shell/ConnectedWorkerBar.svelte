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
	 * tells the app it is shown (native-shell.ts), and its sheet opens the
	 * app's Workers screen, where workers are paired, renamed and removed.
	 */
	import { tick } from 'svelte';
	import { Check, Info, LoaderCircle, Server, Settings, X } from 'lucide-svelte';
	import { listInstalls, type Install } from '$lib/api/installs';
	import { isBundledMobile, nativeCapability } from '$lib/api/runtime';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { streamingSessions } from '$lib/stores/active-session';
	import { notify } from '$lib/stores/notifications';
	import { connectionWorkers, switchRoute, WORKER_SWITCH_CAPABILITY, type ConnectionWorker } from './worker-connection';
	import { announceWorkerBar, hasShellBridge, openNativeWorkers } from './native-shell';

	/** How long "Connecting to" waits before giving the bar back (the app refused, or nothing happened). */
	const CONNECT_TIMEOUT_MS = 5000;

	let installs = $state<Install[]>([]);
	let loaded = $state(false);
	let loadError = $state(false);
	let open = $state(false);
	let connecting = $state<string | null>(null);
	let barRef = $state<HTMLButtonElement | null>(null);
	let sheetRef = $state<HTMLElement | null>(null);

	async function load() {
		try {
			installs = (await listInstalls()).installs;
			loadError = false;
		} catch {
			loadError = true;
		} finally {
			loaded = true;
		}
	}

	$effect(() => { void load(); });

	// The app hides its own worker-name bar while this one is on screen.
	const canManage = hasShellBridge();
	$effect(() => {
		announceWorkerBar(true);
		return () => { announceWorkerBar(false); };
	});

	const streaming = $derived(new Set([...$streamingSessions.keys()]));
	const workers = $derived(connectionWorkers(installs, $sessions, streaming));
	const current = $derived(workers.find((w) => w.current) ?? null);
	const currentName = $derived(current?.name ?? (loaded ? 'this worker' : '…'));

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
		setTimeout(() => { if (connecting === worker.id) connecting = null; }, CONNECT_TIMEOUT_MS);
	}

	function manageWorkers() {
		open = false;
		openNativeWorkers();
	}

	function keydown(event: KeyboardEvent) {
		if (open && event.key === 'Escape' && !event.defaultPrevented) {
			event.preventDefault();
			closeSheet();
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
	aria-label={connecting ? `Connecting to ${workers.find((w) => w.id === connecting)?.name ?? 'worker'}` : `Connected to ${currentName}. Switch worker`}
	onclick={openSheet}
>
	{#if connecting}
		<LoaderCircle size={11} class="conn-spin" aria-hidden="true" />
		<b>{workers.find((w) => w.id === connecting)?.name ?? 'worker'}</b>
	{:else}
		<span class="dot" aria-hidden="true"></span>
		<b>{currentName}</b>
	{/if}
</button>

{#if open}
	<div class="conn-portal" use:portal>
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="scrim" onclick={closeSheet}></div>
		<div bind:this={sheetRef} class="sheet" role="dialog" aria-modal="true" aria-labelledby="conn-title" data-no-drawer-swipe>
			<div class="sheet-h">
				<h4 id="conn-title">Connected to</h4>
				<button type="button" class="icon-btn" aria-label="Close" onclick={closeSheet}><X size={18} aria-hidden="true" /></button>
			</div>
			<p class="sub">The worker this phone shows</p>
			{#if !loaded}
				<p class="empty" role="status"><LoaderCircle size={16} class="conn-spin" aria-hidden="true" />Loading workers</p>
			{:else if loadError && !workers.length}
				<p class="empty" role="alert">Could not load workers.</p>
				<button type="button" class="retry" onclick={() => { loaded = false; void load(); }}>Try again</button>
			{:else}
				<div class="opt-list" role="radiogroup" aria-label="Connected to">
					{#each workers as worker (worker.id)}
						<button type="button" class="opt-row" role="radio" aria-checked={worker.current} disabled={!!connecting} onclick={() => connect(worker)}>
							<span class="wk-ic"><Server size={18} aria-hidden="true" /></span>
							<span class="wk">
								<b>{worker.name}</b>
								<small><span class="dot" class:off={!worker.online} aria-hidden="true"></span>{worker.online ? 'Online' : 'Offline'} · {worker.running} running chat{worker.running === 1 ? '' : 's'}{worker.current ? ' · Current' : ''}</small>
							</span>
							<span class="ck">{#if connecting === worker.id}<LoaderCircle size={18} class="conn-spin" aria-hidden="true" />{:else if worker.current}<Check size={18} aria-hidden="true" />{/if}</span>
						</button>
					{/each}
				</div>
			{/if}
			{#if canManage}
				<button type="button" class="manage" onclick={manageWorkers}><Settings size={16} aria-hidden="true" />Pair, rename or remove workers</button>
			{/if}
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
	.dot.off { background: var(--subtle-foreground); }

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
	.opt-row:disabled { cursor: progress; opacity: 0.7; }
	.opt-row[aria-checked='true'] b { color: var(--primary); }
	.wk-ic { display: grid; place-items: center; width: 34px; height: 34px; border-radius: calc(var(--radius) - 2px); background: var(--muted); color: var(--muted-foreground); }
	.wk { min-width: 0; }
	.wk b { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 550; }
	.wk small { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted-foreground); }
	.ck { color: var(--primary); }
	.empty { display: flex; align-items: center; gap: 8px; padding: 14px 4px; font-size: 13px; color: var(--muted-foreground); }
	.retry { min-height: 44px; padding: 0 14px; border: 1px solid var(--border-strong); border-radius: var(--radius); background: var(--card); color: var(--foreground); font: inherit; cursor: pointer; }
	.manage { display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%; min-height: 44px; margin-top: 10px; padding: 0 14px; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--card); color: var(--foreground); font: inherit; font-size: 13.5px; cursor: pointer; }
	.manage:hover { background: var(--hover-overlay); }
	.manage :global(svg) { color: var(--muted-foreground); }
	.note { display: flex; align-items: center; gap: 8px; margin: 10px 2px 0; font-size: 12.5px; color: var(--muted-foreground); }
	.opt-row:focus-visible, .icon-btn:focus-visible, .retry:focus-visible, .manage:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	:global(.conn-spin) { animation: conn-spin 0.9s linear infinite; }
	@keyframes conn-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.scrim, .sheet, :global(.conn-spin) { animation: none; }
	}
</style>
