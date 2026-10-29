<script lang="ts">
	/** The real projection, grouping and row renderer, without the session network graph. */
	import type { ChatMessage, PendingPeerMessage } from '$lib/stores/types';
	import { createAgentMessageProjection } from '$lib/utils/agent-message-projection';
	import { createTranscriptModelBuilder, type DisplayItem } from '$lib/utils/work-log';
	import { agentDelivery, agentSender, agentSenderNames } from '$lib/utils/agent-delivery';
	import { createFoldState } from '$lib/stores/session/fold-state';
	import AgentMessage from '../AgentMessage.svelte';
	import WorkLogGroup from '../WorkLogGroup.svelte';

	let { sessionId = 'recipient', messages = [], pendingPeerMessages = [], live = true, cancelledTurnIds }: {
		sessionId?: string;
		messages?: ChatMessage[];
		pendingPeerMessages?: PendingPeerMessage[];
		live?: boolean;
		cancelledTurnIds?: string[];
	} = $props();
	const projection = createAgentMessageProjection();
	const buildTranscript = createTranscriptModelBuilder();
	const folds = createFoldState();
	const projected = $derived(projection.update({ sessionId, messages, pendingPeerMessages, sessionActive: live, cancelledTurnIds }));
	const model = $derived(buildTranscript(projected, { sessionId, isStreaming: live, latestTurnUnsettled: live, groupingEnabled: true }));
	const names = $derived(agentSenderNames(messages));
	function itemKey(item: DisplayItem): string {
		switch (item.kind) {
			case 'message': return `message-${item.message.id || item.index}`;
			case 'system_group': return `system-group-${item.messages[0]?.id || item.indices[0] || 0}`;
			case 'work_log': return item.key;
		}
	}
</script>

{#snippet row(message: ChatMessage, _index: number)}
	{@const delivery = agentDelivery(message)}
	{#if delivery}
		{@const sender = agentSender(delivery, names)}
		<AgentMessage sender={sender.name} senderId={sender.id} content={delivery.content} queued={message.systemData?.delivery === 'queued'} />
	{:else}
		<p>{message.content}</p>
	{/if}
{/snippet}

{#each model.items as item (itemKey(item))}
	<div data-item-key={itemKey(item)}>
		{#if item.kind === 'work_log'}
			<WorkLogGroup
				mode={item.mode} folded={item.folded} tail={item.tail} sequence={item.sequence} runs={item.runs}
				foldKey={item.foldKey} foldAlias={item.foldAlias} gallery={item.gallery}
				durationLabel={item.durationLabel} summary={item.summary} compactSummary={item.compactSummary}
				{row} {folds}
			/>
		{:else if item.kind === 'message'}
			{@render row(item.message, item.index)}
		{/if}
	</div>
{/each}
