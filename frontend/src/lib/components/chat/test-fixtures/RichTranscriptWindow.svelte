<script lang="ts">
 import AssistantMessage from '../AssistantMessage.svelte';
 import ToolUseMessage from '../ToolUseMessage.svelte';
 import WorkLogGroup from '../WorkLogGroup.svelte';
 import type { ChatMessage } from '$lib/stores/types';
 import type { DisplayItem } from '$lib/utils/work-log';
 import type { TranscriptRange } from '../transcript-window';
 let { items, range }: { items: DisplayItem[]; range: TranscriptRange } = $props();
</script>
{#snippet row(message: ChatMessage, _index: number)}
 {#if message.role === 'user'}<p>{message.content}</p>
 {:else if message.type === 'text'}<AssistantMessage content={message.content} streaming={false} sessionId="budget-fixture" />
 {:else if message.type === 'tool_use'}<ToolUseMessage name={message.toolName ?? 'Read'} result={message.toolResult} status="complete" />{/if}
{/snippet}
{#each items.slice(range.start, range.end) as item, index (range.start + index)}
 <div class="msg-virtualized">
 {#if item.kind === 'work_log'}
  <WorkLogGroup mode={item.mode} folded={item.folded} tail={item.tail} sequence={item.sequence} runs={item.runs}
   gallery={item.gallery} durationLabel={item.durationLabel} summary={item.summary} {row} pagingEnabled={false} />
 {:else if item.kind === 'message'}{@render row(item.message, item.index)}{/if}
 </div>
{/each}
