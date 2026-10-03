<script lang="ts">
	// The production Sidebar in an app-sized frame. On desktop it sits docked
	// beside a stand-in main view; with `phone` it fills the screen the way the
	// mobile drawer hosts it. The log records where each action would go.
	import Sidebar from '../src/lib/components/layout/Sidebar.svelte';
	import BannerHost from '../src/lib/components/BannerHost.svelte';

	let { phone }: { phone: boolean } = $props();
	let log = $state<string[]>([]);
	const record = (what: string) => (log = [...log, what]);
</script>

<div class="frame" class:phone>
	<aside>
		<Sidebar
			isAdmin
			onNewSession={() => record('new session')}
			onSettledOpenSession={() => record('settled-open')}
			onOpenSession={(id: string) => record(`open session ${id}`)}
			onOpenFeature={(id: string) => record(`open feature ${id}`)}
			onOpenSpotlight={() => record('spotlight')}
		/>
	</aside>
	{#if !phone}
		<main>
			<p>Main view</p>
		</main>
	{/if}
</div>
<ol data-testid="log" hidden>
	{#each log as entry, i (i)}<li>{entry}</li>{/each}
</ol>
<BannerHost />

<style>
	:global(html, body) { margin: 0; height: 100%; overflow: hidden; }
	.frame { display: flex; height: 100dvh; background: var(--background); }
	aside { width: 340px; flex: none; border-right: 1px solid var(--border); position: relative; }
	.frame.phone aside { width: 100%; border-right: 0; }
	main { flex: 1; display: grid; place-items: center; color: var(--muted-foreground); }
</style>
