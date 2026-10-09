<script lang="ts">
	/**
	 * Presence dot for a worker in the runner pickers: green when the worker
	 * is online, red when it is offline. It only reports state; whether an
	 * offline worker can be picked stays with the menu that renders it.
	 *
	 * The dot is decorative to assistive tech: every place that renders it
	 * already says "offline" in its own text, label or description, and a
	 * labelled dot would prefix every menu item's accessible name. Sighted
	 * users get the state as a tooltip.
	 */
	interface Props {
		online: boolean;
	}

	let { online }: Props = $props();

	const label = $derived(online ? 'Online' : 'Offline');
</script>

<span
	class="worker-status-dot"
	class:online
	class:offline={!online}
	data-status={online ? 'online' : 'offline'}
	title={label}
	aria-hidden="true"
></span>

<style>
	.worker-status-dot {
		display: inline-block;
		flex: 0 0 auto;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		align-self: center;
		background: var(--muted-foreground);
	}

	/* A soft halo in the same hue keeps the dot legible on the tinted runner
	   chip and on the popover without adding a hard outline. */
	.worker-status-dot.online {
		background: var(--success);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--success) 22%, transparent);
	}

	.worker-status-dot.offline {
		background: var(--destructive);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--destructive) 22%, transparent);
	}
</style>
