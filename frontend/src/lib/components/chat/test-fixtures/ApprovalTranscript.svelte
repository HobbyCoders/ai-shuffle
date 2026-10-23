<script lang="ts">
	/**
	 * MessageArea's approval rows without the session graph: the same
	 * presentation model and the same dispatch (`isApprovalCard` →
	 * ApprovalRequestCard with `active` from the oldest pending request,
	 * `isApprovalOutcome` → ApprovalOutcome). every-chat-behaviours.test.ts pins
	 * the matching lines in MessageArea's source.
	 */
	import ApprovalRequestCard from '../ApprovalRequestCard.svelte';
	import ApprovalOutcome from '../ApprovalOutcome.svelte';
	import type { PermissionRequest } from '$lib/api/client';
	import type { ChatMessage } from '$lib/stores/types';
	import { buildTranscriptModel } from '$lib/utils/work-log';
	import {
		approvalCardData,
		currentAssistantQuestion,
		isApprovalCard,
		isApprovalOutcome
	} from '$lib/utils/assistant-confirm';

	let { messages, pendingPermissions = [] }: { messages: ChatMessage[]; pendingPermissions?: PermissionRequest[] } =
		$props();

	const activeApprovalId = $derived(currentAssistantQuestion(pendingPermissions)?.request.request_id ?? null);
	const items = $derived(
		buildTranscriptModel(messages, {
			isStreaming: false,
			latestTurnUnsettled: false,
			runningToolIds: new Set(),
			sessionId: 'home',
			swarms: [],
			groupingEnabled: true
		}).items
	);
	// Every row, in order, whether the model placed it on its own or in a fold.
	const rows = $derived(
		items.flatMap((item) =>
			item.kind === 'message'
				? [item.message]
				: item.kind === 'system_group'
					? item.messages
					: item.sequence.map((entry) => entry.message)
		)
	);
</script>

{#each rows as message (message.id)}
	<div data-testid="row" data-id={message.id}>
		{#if isApprovalCard(message)}
			{@const approval = approvalCardData(message)}
			{#if approval}
				<ApprovalRequestCard data={approval} active={approval.request_id === activeApprovalId} />
			{/if}
		{:else if isApprovalOutcome(message)}
			<ApprovalOutcome data={message.systemData} content={message.content} />
		{:else}
			<p data-role={message.role}>{message.content}</p>
		{/if}
	</div>
{/each}
