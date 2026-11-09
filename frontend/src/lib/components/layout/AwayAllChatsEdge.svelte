<script lang="ts">
	/**
	 * A thin band along the top of the window while Away for all chats is on, so
	 * it reads from the corner of the eye on every screen, above open drawers and
	 * dialogs too. It is out of the page's flow and takes no pointer events: it
	 * cannot move or cover anything. The control that turns it off is
	 * AwayAllChatsSwitch.
	 */
	import { awayMode } from '$lib/stores/away-mode.svelte';
</script>

<div
	class="away-edge"
	class:on={awayMode.globalAway}
	aria-hidden="true"
	data-ui-state
	data-ui-away-all={awayMode.globalLoaded ? (awayMode.globalAway ? 'on' : 'off') : undefined}
></div>

<style>
	.away-edge {
		position: fixed;
		top: env(safe-area-inset-top, 0px);
		inset-inline: 0;
		z-index: 2147483000;
		height: 2px;
		background: var(--warning);
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.2s ease-out;
	}
	.away-edge.on { opacity: 1; }

	@media (prefers-reduced-motion: reduce) {
		.away-edge { transition: none; }
	}
</style>
