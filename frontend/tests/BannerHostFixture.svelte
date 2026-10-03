<script lang="ts">
	import BannerHost from '../src/lib/components/BannerHost.svelte';
	import { notify } from '../src/lib/stores/notifications';

	// The archive Undo banner over a stand-in app shell, mounted in RootLayout's
	// order (banner host first, shell after). ?desktop=1 marks the page as the
	// Windows desktop app, so app.css turns the sidebar head and top bar into
	// the window drag strip; the fixture also draws stand-ins for the native
	// caption buttons Electron paints over the page.
	const params = new URLSearchParams(location.search);
	const desktop = params.get('desktop') === '1';
	if (desktop) document.documentElement.classList.add('desktop', 'desktop-win32');

	const w = window as unknown as { __banner: { undo: number; show: (message?: string) => string } };
	w.__banner = {
		undo: 0,
		// The call the sidebar and session card make after archiving
		// (offerUndo), with no auto-dismiss so a slow run cannot race it.
		show(message = 'Archived 1 session') {
			const id = notify.banner(message, {
				severity: 'info',
				persist: false,
				duration_ms: 0,
				action: {
					label: 'Undo',
					onClick: () => {
						notify.dismissLive(id);
						w.__banner.undo++;
					}
				}
			});
			return id;
		}
	};
	w.__banner.show();
</script>

<BannerHost />
<div class="shell">
	<aside class="side">
		<div class="sb-head"><span>AI Shuffle</span><button type="button">New</button></div>
		<p>Fix the flaky upload test</p>
	</aside>
	<section class="main">
		<header class="topbar"><span>Fix the flaky upload test</span><span class="tb-right"><button type="button">Git</button></span></header>
		<p class="timeline">Assistant: archived the session.</p>
	</section>
</div>
{#if desktop}<span class="caption" data-testid="caption-buttons"><i>–</i><i>▢</i><i>✕</i></span>{/if}

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.shell { display: flex; height: 100dvh; }
	.side { width: 260px; flex: none; border-right: 1px solid var(--border); font-size: 13px; }
	.side p { padding: 0 16px; }
	.main { flex: 1; min-width: 0; }
	.sb-head, .topbar { display: flex; align-items: center; justify-content: space-between; height: 52px; padding: 0 16px; border-bottom: 1px solid var(--border); font-size: 13px; font-weight: 600; box-sizing: border-box; }
	.timeline { padding: 24px; font-size: 14px; }
	/* Stand-in for the native caption buttons Electron draws above the page
	   (titleBarOverlay height 52, three 46px buttons on Windows). */
	.caption { position: fixed; top: 0; right: 0; z-index: 100000; display: flex; height: 52px; background: #2b2b2b; }
	.caption i { display: grid; place-items: center; width: 46px; font-style: normal; color: #ddd; }
	@media (max-width: 639px) { .side { display: none; } }
</style>
