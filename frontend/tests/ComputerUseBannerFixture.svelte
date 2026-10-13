<script lang="ts">
	import ComputerUseBanner from '../src/lib/components/ComputerUseBanner.svelte';
	import NotificationDock from '../src/lib/components/NotificationDock.svelte';
	import { computerUse } from '../src/lib/stores/computerUse.svelte';

	// ?state=active|stopped  ?reason=<stop_reason> (with state=stopped, default escape)
	// ?desktop=1 draws Windows caption buttons over the top bar.
	const params = new URLSearchParams(location.search);
	const desktop = params.get('desktop') === '1';
	if (desktop) document.documentElement.classList.add('desktop', 'desktop-win32');
	computerUse.start = () => {};
	computerUse.stop = () => {};
	computerUse.halt = async () => {
		Object.assign(computerUse, { active: false, stopReason: 'user' });
	};
	Object.assign(computerUse, {
		active: params.get('state') !== 'stopped',
		stopReason: params.get('state') === 'stopped' ? (params.get('reason') || 'escape') : null,
		displayLabel: 'Display 2 · 2560×1440', action: 'left_click', busy: true,
		secondsSince: 0, escapeAvailable: true, overlayAvailable: true,
		stopping: false, connectionLost: false
	});
</script>

<div class="shell">
	<header class="topbar">
		<span class="title">Fix the flaky upload test</span>
		{#if desktop}<span class="caption" data-testid="caption-buttons"><i>–</i><i>▢</i><i>✕</i></span>{/if}
	</header>
	<NotificationDock />
	<main class="timeline">
		<p>Assistant: I'll open the app and check the upload dialog.</p>
	</main>
	<footer class="composer">Message the agent…</footer>
</div>
<ComputerUseBanner />

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.shell { display: flex; flex-direction: column; height: 100dvh; }
	.topbar { position: relative; display: flex; align-items: center; height: var(--titlebar-height, 52px); padding: 0 16px; border-bottom: 1px solid var(--border); font-size: 13px; font-weight: 600; }
	/* Stand-in for the native caption buttons Electron draws above the page. */
	.caption { position: fixed; top: 0; right: 0; z-index: 100000; display: flex; height: 40px; background: #2b2b2b; }
	.caption i { display: grid; place-items: center; width: 46px; font-style: normal; color: #ddd; }
	.timeline { flex: 1; padding: 24px; font-size: 14px; }
	.composer { margin: 0 auto 18px; width: min(720px, calc(100vw - 32px)); padding: 16px; border: 1px solid var(--border-strong); border-radius: 14px; color: var(--muted-foreground); font-size: 14px; }
</style>
