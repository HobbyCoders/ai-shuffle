<script lang="ts">
	/**
	 * The Terminal page: shells on this worker that belong to no chat.
	 *
	 * Reached from the navigation, the search palette and Settings > Providers,
	 * so the owner can sign in to a CLI or finish a setup step before any chat
	 * exists. It is the side panel's TerminalPanel in the worker's own scope
	 * (see WORKER_TERMINALS): the same owner-only routes, the same socket and the
	 * same environment scrub. Leaving the page only detaches; the shells keep
	 * running and come back with their scrollback.
	 */
	import { MediaQuery } from 'svelte/reactivity';
	import TerminalPanel from './TerminalPanel.svelte';
	import { WORKER_TERMINALS } from '$lib/stores/terminals.svelte';
	import { platform } from '$lib/stores/platform.svelte';

	// Phones and narrow windows get the side panel's touch layout: bigger
	// targets and the Ctrl/Esc/Tab/arrow key bar.
	const narrow = new MediaQuery('(max-width: 700px)');
	const mobile = $derived(platform.isMobile || narrow.current);
</script>

<div class="terminal-page">
	<TerminalPanel sessionId={WORKER_TERMINALS} {mobile} />
</div>

<style>
	.terminal-page { display: flex; flex-direction: column; flex: 1; height: 100%; min-height: 0; min-width: 0; }
	.terminal-page > :global(.tpanel) { flex: 1; }
</style>
