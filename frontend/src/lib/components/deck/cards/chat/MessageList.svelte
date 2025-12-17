<script lang="ts">
	/**
	 * MessageList - Scrollable container for chat messages with auto-scroll
	 */
	import type { ChatMessage } from '$lib/stores/tabs';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import StreamingIndicator from './StreamingIndicator.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import SystemMessage from '$lib/components/SystemMessage.svelte';

	interface Props {
		messages: ChatMessage[];
		isStreaming?: boolean;
	}

	let { messages, isStreaming = false }: Props = $props();

	let containerElement: HTMLDivElement | undefined = $state();
	let isUserScrolled = $state(false);
	let lastMessageCount = $state(0);

	// Track user scroll interaction
	function handleScroll() {
		if (!containerElement) return;
		const { scrollTop, scrollHeight, clientHeight } = containerElement;
		// Consider user as "scrolled up" if they're more than 100px from bottom
		isUserScrolled = scrollHeight - scrollTop - clientHeight > 100;
	}

	// Auto-scroll to bottom when new messages arrive (unless user scrolled up)
	$effect(() => {
		if (messages.length > lastMessageCount || isStreaming) {
			lastMessageCount = messages.length;
			if (!isUserScrolled && containerElement) {
				// Use requestAnimationFrame for smoother scrolling
				requestAnimationFrame(() => {
					if (containerElement) {
						containerElement.scrollTop = containerElement.scrollHeight;
					}
				});
			}
		}
	});

	// Scroll to bottom when content updates during streaming
	$effect(() => {
		// Check for streaming text content changes
		const lastMessage = messages[messages.length - 1];
		if (lastMessage?.streaming && !isUserScrolled && containerElement) {
			requestAnimationFrame(() => {
				if (containerElement) {
					containerElement.scrollTop = containerElement.scrollHeight;
				}
			});
		}
	});

	// Determine message type and render accordingly
	function getMessageType(message: ChatMessage): 'user' | 'assistant' | 'tool_use' | 'subagent' | 'system' {
		if (message.role === 'user') return 'user';
		if (message.type === 'tool_use') return 'tool_use';
		if (message.type === 'subagent') return 'subagent';
		if (message.type === 'system' || message.role === 'system') return 'system';
		return 'assistant';
	}

	// Check if streaming indicator should show (no streaming message visible)
	const showStreamingIndicator = $derived.by(() => {
		if (!isStreaming) return false;
		// Don't show if we have messages and the last one is already streaming
		if (messages.length > 0) {
			const lastMsg = messages[messages.length - 1];
			if (lastMsg.streaming || lastMsg.type === 'tool_use' && lastMsg.toolStatus === 'running') {
				return false;
			}
		}
		return true;
	});
</script>

<div
	bind:this={containerElement}
	onscroll={handleScroll}
	class="flex-1 overflow-y-auto px-4 py-3 space-y-4 scroll-smooth"
>
	{#if messages.length === 0 && !isStreaming}
		<!-- Empty state -->
		<div class="flex flex-col items-center justify-center h-full text-center py-8">
			<div class="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center mb-3">
				<svg class="w-6 h-6 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
				</svg>
			</div>
			<p class="text-sm text-muted-foreground">Start a conversation</p>
			<p class="text-xs text-muted-foreground/70 mt-1">Type a message below to begin</p>
		</div>
	{:else}
		{#each messages as message (message.id)}
			{@const msgType = getMessageType(message)}
			{#if msgType === 'user'}
				<UserMessage {message} />
			{:else if msgType === 'tool_use'}
				<ToolUseMessage {message} />
			{:else if msgType === 'subagent'}
				<SubagentMessage {message} />
			{:else if msgType === 'system'}
				<SystemMessage {message} />
			{:else}
				<AssistantMessage {message} />
			{/if}
		{/each}

		<!-- Global streaming indicator when needed -->
		{#if showStreamingIndicator}
			<StreamingIndicator />
		{/if}
	{/if}
</div>
