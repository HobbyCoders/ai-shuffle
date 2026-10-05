<script lang="ts">
	import BannerHost from '../src/lib/components/BannerHost.svelte';
	import NotificationDock from '../src/lib/components/NotificationDock.svelte';
	import NotificationSlot from '../src/lib/components/NotificationSlot.svelte';
	import Banner from '../src/lib/components/Banner.svelte';
	import ComputerUseBanner from '../src/lib/components/ComputerUseBanner.svelte';
	import StudioTopBar from '../src/lib/components/layout/StudioTopBar.svelte';
	import { computerUse } from '../src/lib/stores/computerUse.svelte';
	import { DOCK_ORDER } from '../src/lib/stores/notification-dock.svelte';
	import { notify } from '../src/lib/stores/notifications';

	// A stand-in app shell in AppLayout + MainPanel's structure, with the real
	// top bar (the desktop drag strip), the real dock and real notices, mounted
	// in RootLayout's order: the fallback host and the computer-use bar first,
	// the shell after. ?desktop=1 marks the page as the Windows desktop app and
	// draws stand-ins for the native caption buttons. ?sidebar=0 starts with the
	// sidebar closed, ?panel=1 with a side panel open, ?shell=mobile is the phone.
	const params = new URLSearchParams(location.search);
	const desktop = params.get('desktop') === '1';
	const phone = params.get('shell') === 'mobile';
	if (desktop) document.documentElement.classList.add('desktop', 'desktop-win32');

	let sidebarOpen = $state(!phone && params.get('sidebar') !== '0');
	let panelOpen = $state(params.get('panel') === '1');
	let offline = $state(true);
	let span = $state(0);

	computerUse.start = () => {};
	computerUse.stop = () => {};
	computerUse.halt = async () => { Object.assign(computerUse, { active: false, stopReason: 'user' }); };
	Object.assign(computerUse, {
		active: true, stopReason: null, displayLabel: 'Display 2', action: 'left_click', busy: true,
		secondsSince: 0, escapeAvailable: true, overlayAvailable: true, stopping: false, connectionLost: false
	});

	const w = window as unknown as { __dock: Record<string, unknown> };
	w.__dock = {
		undo: 0,
		reconnect: 0,
		show(message = 'Archived 1 session') {
			const id = notify.banner(message, {
				severity: 'info', persist: false, duration_ms: 0,
				action: { label: 'Undo', onClick: () => { notify.dismissLive(id); (w.__dock.undo as number)++; } }
			});
			return id;
		},
		setSidebar(open: boolean) { sidebarOpen = open; },
		setPanel(open: boolean) { panelOpen = open; },
		setOffline(value: boolean) { offline = value; },
		setComputerUse(active: boolean) { Object.assign(computerUse, { active, stopReason: active ? null : 'user' }); },
	};
	(w.__dock.show as () => string)();
</script>

<BannerHost />
<ComputerUseBanner />
<div class="app-shell" class:phone>
	<div class="app-body">
		{#if sidebarOpen}
			<div class="sidebar-slot">
				<aside class="side">
					<header class="p-head"><span>Sessions</span><button type="button">Bell</button></header>
					<p>Fix the flaky upload test</p>
				</aside>
			</div>
		{/if}
		<div class="main-panel">
			<StudioTopBar title="Fix the flaky upload test" projectLabel="ai-shuffle" sidebarCollapsed={!sidebarOpen}
				onToggleSidebar={() => (sidebarOpen = !sidebarOpen)}>
				{#snippet actions()}<button type="button" class="git">Git</button>{/snippet}
			</StudioTopBar>
			<div class="main-content">
				<div class="main-surface" bind:clientWidth={span}>
					<p class="timeline">Assistant: archived the session.</p>
					<footer class="composer">Message the agent…</footer>
				</div>
				{#if panelOpen}<aside class="side-panel">Live browser</aside>{/if}
				<NotificationDock {span} />
			</div>
		</div>
	</div>
</div>
{#if offline}
	<NotificationSlot order={DOCK_ORDER.connection}>
		<Banner severity="warning" variant="page" message="Chat controls disconnected. Reconnect to send messages."
			action={{ label: 'Reconnect', onClick: () => { (w.__dock.reconnect as number)++; offline = false; } }} />
	</NotificationSlot>
{/if}
{#if desktop}<span class="caption" data-testid="caption-buttons"><i>–</i><i>▢</i><i>✕</i></span>{/if}

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	/* AppLayout and MainPanel's boxes. */
	.app-shell { display: flex; flex-direction: column; height: 100dvh; overflow: hidden; }
	.app-body { position: relative; flex: 1; min-height: 0; display: flex; overflow: hidden; }
	.sidebar-slot { flex: none; width: 320px; border-right: 1px solid var(--sidebar-border); background: var(--sidebar); }
	.p-head { display: flex; align-items: center; justify-content: space-between; height: 52px; padding: 0 16px; font-size: 13px; font-weight: 600; }
	.side p { padding: 0 16px; font-size: 13px; }
	.main-panel { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }
	.main-content { position: relative; display: flex; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
	.main-surface { isolation: isolate; display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; overflow: hidden; }
	.side-panel { flex: 0 0 auto; width: 420px; max-width: 70%; border-left: 1px solid var(--border); padding: 16px; font-size: 13px; z-index: 20; }
	.timeline { flex: 1; padding: 24px; font-size: 14px; }
	.composer { margin: 0 auto 18px; width: min(720px, calc(100% - 32px)); padding: 16px; border: 1px solid var(--border-strong); border-radius: 14px; color: var(--muted-foreground); font-size: 14px; box-sizing: border-box; }
	.git { height: 30px; padding: 0 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--secondary); color: inherit; }
	/* Stand-in for the native caption buttons Electron draws above the page
	   (titleBarOverlay height 52, three 46px buttons on Windows). */
	.caption { position: fixed; top: 0; right: 0; z-index: 100000; display: flex; height: 52px; background: #2b2b2b; }
	.caption i { display: grid; place-items: center; width: 46px; font-style: normal; color: #ddd; }
</style>
