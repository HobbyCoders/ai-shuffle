<script lang="ts">
	/**
	 * SystemMessageGroup - Collapsible container for 2+ consecutive system/hook messages.
	 * Mirrors ToolUseGroup: shows a summary header with a count and expands to reveal
	 * individual SystemMessage components. Declutters the chat when many low-signal
	 * hook events (hook_started, hook_response, turn_duration, task_*, etc.) arrive
	 * back-to-back.
	 */
	import type { ChatMessage } from '$lib/stores/types';
	import SystemMessage from '$lib/components/SystemMessage.svelte';

	interface Props {
		messages: ChatMessage[];
	}

	let { messages }: Props = $props();

	// ToolUseGroup-style persisted expand: bind the <details> open state to
	// component state so a user's expand survives streaming re-renders (an
	// unbound `open` attribute resets to collapsed whenever the group's
	// children re-render mid-stream, snapping the panel shut while reading).
	let detailsOpen = $state(false);

	// If every message is a hook_* subtype, label the group "hooks".
	// Otherwise use the more generic "system messages".
	const allHooks = $derived(
		messages.every((m) => (m.systemSubtype || '').startsWith('hook_'))
	);

	// If every message is a background-task subtype, label the group as task
	// updates and swap the header to the bot icon (matching the task rows).
	const TASK_SUBTYPES = ['task_started', 'task_progress', 'task_updated', 'task_notification'];
	const allTasks = $derived(
		messages.every((m) => TASK_SUBTYPES.includes(m.systemSubtype || ''))
	);

	const label = $derived(
		allTasks
			? `${messages.length} task ${messages.length === 1 ? 'update' : 'updates'}`
			: allHooks
				? `${messages.length} ${messages.length === 1 ? 'hook' : 'hooks'}`
				: `${messages.length} system ${messages.length === 1 ? 'message' : 'messages'}`
	);

	// Consider the group to be in an error state if any child has an error-ish
	// subtype. The full badge logic lives in SystemMessage itself; here we just
	// surface a coarse indicator for the summary row.
	const hasError = $derived(
		messages.some((m) => {
			const s = m.systemSubtype || '';
			return (
				s.includes('error') ||
				s.includes('fail') ||
				s === 'plugin_warning' ||
				(s === 'hook_response' &&
					typeof m.systemData === 'object' &&
					m.systemData !== null &&
					// exit code non-zero = failed hook
					((m.systemData as Record<string, unknown>).exitCode !== undefined
						? (m.systemData as Record<string, unknown>).exitCode !== 0
						: (m.systemData as Record<string, unknown>).exit_code !== undefined
							? (m.systemData as Record<string, unknown>).exit_code !== 0
							: (m.systemData as Record<string, unknown>).success === false))
			);
		})
	);
</script>

<div class="system-message-group">
	<details class="sg-details" bind:open={detailsOpen}>
		<summary class="sg-row">
			{#if allTasks}
				<!-- Task icon (bot) matches individual task rows -->
				<svg class="sg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<rect x="3" y="11" width="18" height="10" rx="2" />
					<circle cx="12" cy="5" r="2" />
					<path d="M12 7v4" />
					<line x1="8" y1="16" x2="8" y2="16" />
					<line x1="16" y1="16" x2="16" y2="16" />
				</svg>
			{:else}
				<!-- Hook icon (lightning bolt) matches individual SystemMessage rows -->
				<svg class="sg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
			{/if}

			<span class="sg-label">{label}</span>

			<span class="sg-right">
				<svg class="sg-chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="m6 9 6 6 6-6" />
				</svg>
				{#if hasError}
					<svg class="sg-glyph is-error" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				{:else}
					<svg class="sg-glyph is-ok" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<path d="M20 6 9 17l-5-5" />
					</svg>
				{/if}
			</span>
		</summary>
		<div class="sg-body">
			{#each messages as m (m.id)}
				<SystemMessage message={m} />
			{/each}
		</div>
	</details>
</div>

<style>
	.system-message-group {
		width: 100%;
		min-width: 0;
		margin: 0;
	}

	.sg-details {
		min-width: 0;
	}

	.sg-row {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		padding: 6px 8px;
		border-radius: 8px;
		cursor: pointer;
		list-style: none;
		transition: background-color 0.13s ease;
	}

	.sg-row::-webkit-details-marker {
		display: none;
	}

	.sg-row:hover {
		background: var(--scrim);
	}

	.sg-row:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.sg-icon {
		flex: none;
		color: var(--subtle-foreground);
	}

	.sg-label {
		font-size: 12.5px;
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		min-width: 0;
	}

	.sg-right {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 8px;
		flex: none;
		color: var(--subtle-foreground);
	}

	.sg-chev {
		transition: transform 0.16s ease;
	}

	.sg-details[open] .sg-chev {
		transform: rotate(180deg);
	}

	.sg-glyph.is-ok { color: var(--emerald); }
	.sg-glyph.is-error { color: var(--rose); }

	.sg-body {
		margin: 2px 0 4px 14px;
		padding-left: 12px;
		border-left: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
</style>
