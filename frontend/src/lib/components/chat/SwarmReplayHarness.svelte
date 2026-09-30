<script lang="ts">
 import { swarms } from '$lib/stores/swarms';
 import { buildDisplayItems } from '$lib/utils/work-log';
 import type { ChatMessage } from '$lib/stores/types';
 import ToolUseMessage from './ToolUseMessage.svelte';
 import SwarmDropdown from './SwarmDropdown.svelte';
 import WorkLogGroup from './WorkLogGroup.svelte';
 let { messages, sessionId, groupingEnabled }: {
  messages: ChatMessage[]; sessionId: string; groupingEnabled: boolean;
 } = $props();
 const items = $derived(buildDisplayItems(messages, { groupingEnabled, isStreaming: false,
  sessionId, swarms: Object.values($swarms.bySession[sessionId] ?? {}) }));
</script>
{#snippet row(message: ChatMessage)}
 {#if message.type === 'tool_use'}
  <ToolUseMessage {sessionId} name={message.toolName!} input={message.toolInput}
   result={message.toolResult} status={message.toolStatus!} />
 {/if}
{/snippet}
<SwarmDropdown {sessionId} />
{#each items as item, index (index)}
 {#if item.kind === 'message'}
  {@render row(item.message)}
 {:else if item.kind === 'work_log'}
  <WorkLogGroup mode={item.mode} folded={item.folded} tail={item.tail}
   sequence={item.sequence} {row} />
 {/if}
{/each}
