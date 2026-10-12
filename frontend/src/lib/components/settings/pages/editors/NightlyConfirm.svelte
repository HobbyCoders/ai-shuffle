<script lang="ts">
	/**
	 * NightlyConfirm: the step before a worker moves to the nightly channel,
	 * opened under System > Updates > Update channel. It repeats the one cost
	 * that cannot be undone from here. Focus starts on Cancel; Esc cancels.
	 */
	import { REINSTALL_TEXT } from '../update-channel.svelte';

	interface Props {
		onConfirm: () => void;
		onCancel: () => void;
	}

	let { onConfirm, onCancel }: Props = $props();

	const uid = $props.id();
	let cancel = $state<HTMLButtonElement | null>(null);
	let box = $state<HTMLDivElement | null>(null);

	$effect(() => {
		cancel?.focus({ preventScroll: true });
		box?.scrollIntoView?.({ block: 'nearest' });
	});

	function keydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		event.stopPropagation();
		onCancel();
	}
</script>

<!-- Esc anywhere in the step cancels it. -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	class="nc"
	bind:this={box}
	role="group"
	aria-labelledby="{uid}-q"
	aria-describedby="{uid}-why"
	onkeydown={keydown}
>
	<span class="nc-rail" aria-hidden="true"></span>
	<div class="nc-body">
		<p class="nc-q" id="{uid}-q">Move this worker to nightly?</p>
		<p class="nc-why" id="{uid}-why">
			Nightly has the newest changes and may break. <strong>{REINSTALL_TEXT}</strong>
		</p>
		<div class="st-acts">
			<button type="button" class="st-btn ghost" bind:this={cancel} onclick={onCancel}>Cancel</button>
			<button type="button" class="st-btn warn" onclick={onConfirm}>Switch to nightly</button>
		</div>
	</div>
</div>

<style>
	.nc {
		display: flex;
		gap: 12px;
		padding: 12px 14px 12px 12px;
		border-radius: var(--st-radius-md);
		background: var(--st-warning-soft);
		animation: nc-in 0.18s ease-out;
	}
	.nc-rail {
		flex: none;
		width: 3px;
		border-radius: 2px;
		background: var(--st-warning);
	}
	.nc-body {
		min-width: 0;
		flex: 1;
	}
	.nc-q {
		margin: 0;
		font-size: 13.5px;
		font-weight: 600;
		color: var(--st-fg);
	}
	.nc-why {
		margin: 4px 0 0;
		font-size: 13px;
		line-height: 1.5;
		color: var(--st-mfg);
	}
	.nc-why strong {
		font-weight: 500;
		color: var(--st-fg);
	}
	.nc .st-acts {
		margin-top: 10px;
	}
	@keyframes nc-in {
		from {
			opacity: 0;
			transform: translateY(-3px);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.nc {
			animation: none;
		}
	}
</style>
