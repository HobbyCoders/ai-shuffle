<script lang="ts">
	/**
	 * StopButton — the informed-choice Stop control for a streaming turn.
	 *
	 * The interrupt is query-scoped and has no task filter — the CLI cancels the
	 * WHOLE query, including every in-flight background Task child. "Stop the
	 * foreground but keep the agents" is impossible at that layer, so when
	 * running agents exist Stop is an INFORMED choice: a confirm warns that
	 * stopping takes them down too. Plain Stop with no background work is one
	 * click, immediate, zero friction. Either way the interrupt is a FULL stop.
	 */
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import type { Task } from '$lib/stores/types';

	interface Props {
		/** The session's live background tasks (from /live `activeTasks`). */
		activeTasks: Task[];
		/** Fire the interrupt (always a full stop). */
		onStop: () => void;
	}

	let { activeTasks, onStop }: Props = $props();

	let showConfirm = $state(false);

	// Only RUNNING tasks can be stopped and therefore matter for the informed
	// choice — terminal (killed/error/completed) rows the registry keeps around for
	// its retention window must not add friction to Stop.
	const runningCount = $derived(activeTasks.filter((t) => t.status === 'running').length);

	const confirmMessage = $derived(
		runningCount === 1
			? '1 background agent is still running. Stopping will also stop it.'
			: `${runningCount} background agents are still running. Stopping will also stop them.`
	);

	function handleClick() {
		if (runningCount > 0) {
			showConfirm = true;
		} else {
			// No background work: interrupt immediately.
			onStop();
		}
	}

	function confirmStop() {
		showConfirm = false;
		onStop();
	}

	function cancelStop() {
		showConfirm = false;
	}
</script>

<button type="button" onclick={handleClick} class="stop-btn" title="Stop generating" aria-label="Stop generating">
	<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
		<rect x="6" y="6" width="12" height="12" rx="2" />
	</svg>
</button>

<ConfirmDialog
	open={showConfirm}
	title="Stop background agents too?"
	message={confirmMessage}
	confirmLabel="Stop everything"
	cancelLabel="Cancel"
	danger={true}
	onConfirm={confirmStop}
	onCancel={cancelStop}
/>

<style>
	.stop-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: var(--status-error-bg);
		color: var(--destructive);
		border-radius: calc(var(--radius, 0.75rem) * 0.67);
		transition: background-color 0.15s;
	}

	.stop-btn:hover {
		background: color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	.stop-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
</style>
