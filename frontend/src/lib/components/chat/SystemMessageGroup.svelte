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

	// If every message is a hook_* subtype, label the group "hooks".
	// Otherwise use the more generic "system messages".
	const allHooks = $derived(
		messages.every((m) => (m.systemSubtype || '').startsWith('hook_'))
	);

	const label = $derived(
		allHooks
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

<div class="w-full system-message-group">
	<div class="min-w-0">
		<details class="w-full border border-border rounded-lg overflow-hidden group">
			<summary class="system-group-summary w-full px-3 py-2 flex items-center gap-2 cursor-pointer list-none transition-colors">
				<!-- Status indicator -->
				{#if hasError}
					<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				{:else}
					<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{/if}
				<!-- Hook icon (lightning bolt) matches individual SystemMessage rows -->
				<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
					<path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
				<!-- Summary text -->
				<span class="text-sm font-medium text-foreground flex-shrink-0">
					{label}
				</span>
				<!-- Chevron -->
				<svg class="w-4 h-4 text-muted-foreground transition-transform group-open:rotate-180 flex-shrink-0 ml-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</summary>
			<div class="px-3 pt-2 pb-3 space-y-2">
				{#each messages as m (m.id)}
					<SystemMessage message={m} />
				{/each}
			</div>
		</details>
	</div>
</div>

<style>
	.system-group-summary:hover {
		background: var(--hover-overlay);
	}
</style>
