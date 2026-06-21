<script lang="ts">
	import { MessageSquare, Brain } from 'lucide-svelte';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences.svelte';

	interface Props {
		chatPrefs: ChatPreferences | null;
	}

	let { chatPrefs }: Props = $props();
</script>

{#if chatPrefs}
	<!-- Reasoning & tools -->
	<div class="set-group">
		<div class="set-grouphead">
			<h3><Brain />Reasoning &amp; tools</h3>
			<p>How thinking blocks, tool results and todos appear in the transcript.</p>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Show thinking blocks</h4><p>Display Claude's reasoning process.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.showThinkingBlocks}
					role="switch" aria-checked={chatPrefs.showThinkingBlocks} aria-label="Show thinking blocks"
					onclick={() => chatPreferences.setShowThinkingBlocks(!chatPrefs?.showThinkingBlocks)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Expand thinking by default</h4><p>Thinking blocks start expanded.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.thinkingStartExpanded}
					disabled={!chatPrefs.showThinkingBlocks}
					role="switch" aria-checked={chatPrefs.thinkingStartExpanded} aria-label="Expand thinking by default"
					onclick={() => chatPreferences.setThinkingStartExpanded(!chatPrefs?.thinkingStartExpanded)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Show thinking summary</h4><p>Preview thinking content when collapsed.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.showThinkingSummary}
					disabled={!chatPrefs.showThinkingBlocks}
					role="switch" aria-checked={chatPrefs.showThinkingSummary} aria-label="Show thinking summary"
					onclick={() => chatPreferences.setShowThinkingSummary(!chatPrefs?.showThinkingSummary)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Expand tools by default</h4><p>Tool results start expanded.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.toolsStartExpanded}
					role="switch" aria-checked={chatPrefs.toolsStartExpanded} aria-label="Expand tools by default"
					onclick={() => chatPreferences.setToolsStartExpanded(!chatPrefs?.toolsStartExpanded)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Expand todo list by default</h4><p>Task list starts expanded.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.todosStartExpanded}
					role="switch" aria-checked={chatPrefs.todosStartExpanded} aria-label="Expand todo list by default"
					onclick={() => chatPreferences.setTodosStartExpanded(!chatPrefs?.todosStartExpanded)}><i></i></button>
			</div>
		</div>
	</div>

	<!-- Messages -->
	<div class="set-group">
		<div class="set-grouphead">
			<h3><MessageSquare />Messages</h3>
			<p>Scrolling, timestamps and message density.</p>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Auto-scroll on new messages</h4><p>Automatically scroll to new content.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.autoScrollOnNewMessage}
					role="switch" aria-checked={chatPrefs.autoScrollOnNewMessage} aria-label="Auto-scroll on new messages"
					onclick={() => chatPreferences.setAutoScrollOnNewMessage(!chatPrefs?.autoScrollOnNewMessage)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Show message timestamps</h4><p>Display the time on each message.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.showMessageTimestamps}
					role="switch" aria-checked={chatPrefs.showMessageTimestamps} aria-label="Show message timestamps"
					onclick={() => chatPreferences.setShowMessageTimestamps(!chatPrefs?.showMessageTimestamps)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Compact message view</h4><p>Reduce spacing between messages.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.compactMessageView}
					role="switch" aria-checked={chatPrefs.compactMessageView} aria-label="Compact message view"
					onclick={() => chatPreferences.setCompactMessageView(!chatPrefs?.compactMessageView)}><i></i></button>
			</div>
		</div>
	</div>
{:else}
	<div class="set-group">
		<div class="set-row"><div class="lab"><p>Loading preferences…</p></div></div>
	</div>
{/if}

<style>
	.switch:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
</style>
