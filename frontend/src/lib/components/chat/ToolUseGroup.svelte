<script lang="ts">
	/**
	 * ToolUseGroup - Collapsible container for 2+ tool calls from the same assistant turn.
	 * Shows a summary header with completion status and expands to reveal individual tools.
	 */
	import type { ChatMessage } from '$lib/stores/types';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import ToolScreenshot from './ToolScreenshot.svelte';
	import { isDismissedQuestion } from './question-outcome';

	interface Props {
		tools: ChatMessage[];
	}

	let { tools }: Props = $props();

	// Tools whose result carried a screenshot. These are hoisted out of the
	// collapsible group and rendered below it — otherwise a screenshot is hidden
	// inside the collapsed <details> where the user would never see it.
	const screenshotTools = $derived(tools.filter(t => t.toolScreenshot));

	let userToggled = $state(false);
	let detailsOpen = $state(false);

	// A dismissed AskUserQuestion is denied, not failed — it must not colour the
	// group red or inflate the "(n failed)" count. See ./question-outcome.ts.
	const isFailure = (t: ChatMessage) =>
		t.toolStatus === 'error' && !isDismissedQuestion(t.toolName, t.toolStatus, t.toolResult);

	const groupStatus = $derived.by(() => {
		if (tools.some(t => t.toolStatus === 'running' || t.streaming)) return 'running';
		if (tools.some(isFailure)) return 'error';
		return 'complete';
	});

	const completedCount = $derived(
		tools.filter(t => t.toolStatus === 'complete' || (t.toolStatus === 'error' && !isFailure(t))).length
	);

	const errorCount = $derived(tools.filter(isFailure).length);

	const finishedCount = $derived(completedCount + errorCount);

	function handleToggle() {
		userToggled = true;
	}
</script>

<div class="w-full tool-use-group">
	<div class="min-w-0">
		<details class="w-full border border-border rounded-lg overflow-hidden group" bind:open={detailsOpen} ontoggle={handleToggle}>
			<summary class="tool-group-summary w-full px-3 py-2 flex items-center gap-2 cursor-pointer list-none transition-colors">
				<!-- Status indicator -->
				{#if groupStatus === 'running'}
					<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
					</svg>
				{:else if groupStatus === 'error'}
					<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				{:else}
					<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{/if}
				<!-- Tool stack icon -->
				<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
					<rect x="4" y="4" width="16" height="4" rx="1" />
					<rect x="4" y="10" width="16" height="4" rx="1" />
					<rect x="4" y="16" width="16" height="4" rx="1" />
				</svg>
				<!-- Summary text -->
				<span class="text-sm font-medium text-foreground flex-shrink-0">
					{completedCount}/{tools.length} tools
				</span>
				{#if errorCount > 0}
					<span class="text-xs text-red-500 flex-shrink-0">({errorCount} failed)</span>
				{/if}
				<!-- Chevron -->
				<svg class="w-4 h-4 text-muted-foreground transition-transform group-open:rotate-180 flex-shrink-0 ml-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</summary>
			<div class="px-3 pt-2 pb-3 space-y-2">
				{#each tools as tool (tool.id)}
					<!-- screenshot intentionally omitted here — hoisted below the group -->
					<ToolUseMessage
						name={tool.toolName || 'Tool'}
						input={tool.toolInput}
						partialInput={tool.partialToolInput}
						result={tool.toolResult}
						status={tool.toolStatus || 'running'}
						streaming={tool.streaming}
						serverSide={tool.serverSide ?? tool.server_side ?? false}
					/>
				{/each}
			</div>
		</details>
		<!-- Screenshots hoisted out of the collapsed group so they stay visible. -->
		{#each screenshotTools as tool (tool.id)}
			<ToolScreenshot screenshot={tool.toolScreenshot ?? ''} name={tool.toolName || 'Tool'} />
		{/each}
	</div>
</div>

<style>
	.tool-group-summary:hover {
		background: var(--hover-overlay);
	}
</style>
