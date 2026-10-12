<script lang="ts">
	/**
	 * MessageArea's item loop without the session graph: the same keyed `{#each}`
	 * over the presentation model and the same WorkLogGroup props, so a test can
	 * prove which rows keep their DOM node when a turn settles.
	 */
	import WorkLogGroup from '../WorkLogGroup.svelte';
	import type { ChatMessage } from '$lib/stores/types';
	import type { DisplayItem } from '$lib/utils/work-log';
	import type { FoldState } from '$lib/stores/session/fold-state';

	let { items, folds }: { items: DisplayItem[]; folds: FoldState } = $props();

	function itemKey(item: DisplayItem): string {
		switch (item.kind) {
			case 'message':
				return `message-${item.message.id || item.index}`;
			case 'system_group':
				return `system-group-${item.messages[0]?.id || item.indices[0] || 0}`;
			case 'work_log':
				return item.key;
		}
	}
</script>

{#snippet row(message: ChatMessage, _index: number)}
	<div data-testid="work-row" data-id={message.id}>
		<button type="button" onclick={(event) => { (event.currentTarget.parentElement as HTMLElement).dataset.expanded = 'true'; }}>
			{message.content || message.agentName || message.toolName}
		</button>
	</div>
{/snippet}

{#each items as item (itemKey(item))}
	{#if item.kind === 'work_log'}
		<WorkLogGroup
			mode={item.mode}
			folded={item.folded}
			tail={item.tail}
			sequence={item.sequence}
			runs={item.runs}
			foldKey={item.foldKey}
			foldAlias={item.foldAlias}
			gallery={item.gallery}
			durationLabel={item.durationLabel}
			summary={item.summary}
			compactSummary={item.compactSummary}
			{row}
			{folds}
		/>
	{:else if item.kind === 'message'}
		{@render row(item.message, item.index)}
	{/if}
{/each}
