<script lang="ts">
	/**
	 * StopButton — the informed-choice Stop control for a streaming turn.
	 *
	 * Plain Stop with no background work: one click interrupts immediately, exactly
	 * as before (zero friction). But the SDK `interrupt()` is query-scoped and has
	 * no task filter — the CLI cancels the WHOLE query, including every in-flight
	 * background Task child (they are async children inside the one query, not
	 * separate processes). "Stop the foreground but keep the agents" is therefore
	 * impossible at the SDK layer. A user recently lost 5 running agents to a silent
	 * Stop. So when running background agents exist, Stop becomes an INFORMED choice:
	 * confirm that stopping takes them down too, or cancel.
	 *
	 * We deliberately do NOT offer a "keep agents running / detach" option — that
	 * trade-off (the foreground turn keeps burning tokens invisibly) was explicitly
	 * deferred.
	 */
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import type { Task } from '$lib/stores/types';

	interface Props {
		/** The session's live background tasks (from /live `activeTasks`). */
		activeTasks: Task[];
		/** Fire the real interrupt — identical to today's Stop behavior. */
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
	danger
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
