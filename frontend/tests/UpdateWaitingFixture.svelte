<script lang="ts">
	import DesktopUpdateButton from '../src/lib/components/layout/sidebar/DesktopUpdateButton.svelte';
	import NotificationDock from '../src/lib/components/NotificationDock.svelte';

	// The real update button in a sidebar header, and the real anchored dock its
	// restart panel and bell notices draw in. ?theme=light|dark sets the theme
	// class the app's theme store sets on <html>. verify-update-waiting.mjs
	// answers the update API per scenario.
	const params = new URLSearchParams(location.search);
	document.documentElement.classList.add(params.get('theme') === 'light' ? 'light' : 'dark');
	let span = $state(0);
</script>

<div class="shell">
	<aside class="side">
		<header class="strip">
			<span class="brand">AI Shuffle</span>
			<DesktopUpdateButton />
		</header>
		<p class="chat">Fix login</p>
		<p class="chat">Write docs</p>
	</aside>
	<main class="main" bind:clientWidth={span}>
		<p class="timeline">Assistant: the preview server is running on port 5173.</p>
		<NotificationDock {span} />
	</main>
</div>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.shell { display: flex; height: 100dvh; overflow: hidden; }
	.side { flex: none; width: 280px; border-right: 1px solid var(--sidebar-border, var(--border)); background: var(--sidebar, var(--background)); }
	.strip { display: flex; align-items: center; gap: 8px; height: 52px; padding: 0 12px; box-sizing: border-box; border-bottom: 1px solid var(--border); }
	.brand { flex: 1; font-size: 13px; font-weight: 600; }
	.chat { margin: 0; padding: 10px 16px; font-size: 13px; color: var(--muted-foreground); }
	.main { position: relative; flex: 1; min-width: 0; }
	.timeline { padding: 420px 24px 24px; margin: 0; font-size: 14px; color: var(--muted-foreground); }
	@media (max-width: 639px) {
		.shell { flex-direction: column; }
		.side { width: auto; border-right: 0; }
		.chat { display: none; }
		.main { flex: 1; }
	}
</style>
