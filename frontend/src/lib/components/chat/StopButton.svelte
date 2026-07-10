<script lang="ts">
	/**
	 * StopButton — the informed-choice Stop control for a streaming turn.
	 *
	 * Behavior depends on the backend-resolved `preserveBackground` flag (from
	 * /live `preserve_background_on_interrupt`):
	 *
	 * Flag OFF (default — legacy behavior):
	 *   The SDK `interrupt()` is query-scoped and has no task filter — the CLI
	 *   cancels the WHOLE query, including every in-flight background Task child.
	 *   "Stop the foreground but keep the agents" is impossible at that layer, so
	 *   when running agents exist Stop is an INFORMED choice: a confirm warns that
	 *   stopping takes them down too. Plain Stop with no background work is one
	 *   click, immediate, zero friction. Either way the interrupt is a FULL stop —
	 *   we call `onStop(true)`. The backend ignores `stop_background` when the flag
	 *   is off, so the semantics are exactly today's.
	 *
	 * Flag ON (scoped-interrupt patch verified):
	 *   Stop matches Claude Code's Esc — it stops only the foreground turn and
	 *   LEAVES background sub-agents running by DEFAULT. When agents are live the
	 *   confirm's primary action is the safe default (`onStop(false)`), with an
	 *   opt-in checkbox to ALSO stop the agents (`onStop(true)`). With no agents
	 *   running, Stop is immediate and foreground-only (`onStop(false)`).
	 */
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import type { Task } from '$lib/stores/types';

	interface Props {
		/** The session's live background tasks (from /live `activeTasks`). */
		activeTasks: Task[];
		/** When true, Stop preserves background agents by default (Claude Code's
		 *  Esc). When false (default), Stop is a full interrupt as before. */
		preserveBackground?: boolean;
		/**
		 * Fire the interrupt. `stopBackground` tells the backend whether to also
		 * tear down in-flight background sub-agents (true) or stop only the
		 * foreground turn (false). Ignored by the backend when the preserve flag
		 * is off, so the flag-OFF path always passes true.
		 */
		onStop: (stopBackground: boolean) => void;
	}

	let { activeTasks, preserveBackground = false, onStop }: Props = $props();

	let showConfirm = $state(false);
	// Opt-in (flag ON only): also stop the background agents on confirm. Reset each
	// time the confirm opens so a prior tick never leaks into the next Stop.
	let alsoStopBackground = $state(false);

	// Only RUNNING tasks can be stopped and therefore matter for the informed
	// choice — terminal (killed/error/completed) rows the registry keeps around for
	// its retention window must not add friction to Stop.
	const runningCount = $derived(activeTasks.filter((t) => t.status === 'running').length);

	const confirmTitle = $derived(
		preserveBackground ? 'Stop this response?' : 'Stop background agents too?'
	);

	const confirmMessage = $derived(
		preserveBackground
			? runningCount === 1
				? '1 background agent will keep running.'
				: `${runningCount} background agents will keep running.`
			: runningCount === 1
				? '1 background agent is still running. Stopping will also stop it.'
				: `${runningCount} background agents are still running. Stopping will also stop them.`
	);

	const confirmLabel = $derived(preserveBackground ? 'Stop response' : 'Stop everything');
	const checkboxLabel = $derived(preserveBackground ? 'Also stop background agents' : undefined);
	// Flag OFF: always destructive (full stop). Flag ON: only destructive once the
	// user opts into taking the agents down too; the safe default reads as normal.
	const confirmDanger = $derived(preserveBackground ? alsoStopBackground : true);

	function handleClick() {
		if (runningCount > 0) {
			alsoStopBackground = false;
			showConfirm = true;
		} else {
			// No background work: interrupt immediately. Flag OFF → full stop (the
			// field is ignored server-side); flag ON → foreground-only.
			onStop(preserveBackground ? false : true);
		}
	}

	function confirmStop() {
		showConfirm = false;
		// Flag OFF: the interrupt is always a full stop. Flag ON: default keeps
		// agents (false); the opt-in checkbox escalates to a full stop (true).
		onStop(preserveBackground ? alsoStopBackground : true);
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
	title={confirmTitle}
	message={confirmMessage}
	{confirmLabel}
	cancelLabel="Cancel"
	danger={confirmDanger}
	{checkboxLabel}
	bind:checkboxChecked={alsoStopBackground}
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
