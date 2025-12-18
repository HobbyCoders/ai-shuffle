<script lang="ts">
	/**
	 * MessageArea - Scrollable message container with auto-scroll behavior
	 */
	import { tick } from 'svelte';
	import { tabs, profiles, type ChatTab, type ChatMessage } from '$lib/stores/tabs';
	import UserMessage from './UserMessage.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import ToolUseMessage from './ToolUseMessage.svelte';
	import ToolResultMessage from './ToolResultMessage.svelte';
	import SystemMessage from '$lib/components/SystemMessage.svelte';
	import SubagentMessage from '$lib/components/SubagentMessage.svelte';
	import TodoList from '$lib/components/TodoList.svelte';

	interface Props {
		tab: ChatTab;
		onFork?: (sessionId: string, messageIndex: number, messageId: string) => void;
	}

	let { tab, onFork }: Props = $props();

	let containerRef = $state<HTMLDivElement | null>(null);
	let userScrolledUp = $state(false);

	// Current profile for message settings
	const currentProfile = $derived($profiles.find(p => p.id === tab.profile));
	const hasPartialMessages = $derived(currentProfile?.config?.include_partial_messages !== false);

	// Auto-scroll logic
	function isNearBottom(): boolean {
		if (!containerRef) return true;
		const threshold = 100;
		return containerRef.scrollHeight - containerRef.scrollTop - containerRef.clientHeight < threshold;
	}

	function scrollToBottom() {
		if (containerRef) {
			containerRef.scrollTop = containerRef.scrollHeight;
		}
	}

	function handleScroll() {
		userScrolledUp = !isNearBottom();
	}

	// Watch for message changes and auto-scroll
	$effect(() => {
		// Track messages length to trigger on new messages
		const _messagesLength = tab.messages.length;
		if (!userScrolledUp && containerRef) {
			tick().then(scrollToBottom);
		}
	});

	// Scroll on tab change
	$effect(() => {
		const _tabId = tab.id;
		userScrolledUp = false;
		tick().then(scrollToBottom);
	});
</script>

<div
	bind:this={containerRef}
	onscroll={handleScroll}
	class="flex-1 overflow-y-auto"
>
	{#if tab.messages.length === 0}
		<!-- Empty State -->
		<div class="h-full flex items-center justify-center">
			<div class="text-center max-w-md px-6">
				<div class="w-16 h-16 mx-auto mb-6 rounded-2xl bg-primary/10 flex items-center justify-center shadow-s">
					<svg class="w-8 h-8 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="1.5"
							d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
						/>
					</svg>
				</div>
				<h2 class="text-xl font-semibold text-foreground mb-2">Start a Conversation</h2>
				<p class="text-muted-foreground mb-4">Ask Claude anything - code, questions, ideas, or just chat.</p>
				{#if currentProfile}
					<div class="text-sm text-muted-foreground">
						<span>Profile:</span>
						<span class="text-foreground ml-1">{currentProfile.name}</span>
					</div>
				{/if}
			</div>
		</div>
	{:else}
		<!-- Messages -->
		<div class="max-w-5xl mx-auto px-4 sm:px-8 py-4 space-y-4">
			{#each tab.messages as message, messageIndex (message.id)}
				{#if message.role === 'user'}
					<UserMessage content={message.content} />
				{:else if message.type === 'text' || !message.type}
					<AssistantMessage
						content={message.content}
						messageIndex={messageIndex}
						messageId={message.id}
						sessionId={tab.sessionId}
						streaming={message.streaming}
						{hasPartialMessages}
						metadata={message.metadata}
						onFork={onFork}
					/>
				{:else if message.type === 'tool_use' && message.toolName === 'TodoWrite'}
					<!-- TodoWrite - Render as TodoList component -->
					{#if message.toolInput?.todos && Array.isArray(message.toolInput.todos)}
						<div class="w-full">
							<div class="min-w-0">
								<TodoList todos={message.toolInput.todos} />
							</div>
						</div>
					{/if}
				{:else if message.type === 'tool_use'}
					<ToolUseMessage
						name={message.toolName || 'Tool'}
						input={message.toolInput}
						partialInput={message.partialToolInput}
						result={message.toolResult}
						status={message.toolStatus || 'running'}
						streaming={message.streaming}
					/>
				{:else if message.type === 'tool_result'}
					<ToolResultMessage
						content={message.content}
						toolId={message.toolId}
					/>
				{:else if message.type === 'system'}
					<div class="w-full">
						<div class="min-w-0">
							<SystemMessage {message} />
						</div>
					</div>
				{:else if message.type === 'subagent'}
					<div class="w-full">
						<div class="min-w-0">
							<SubagentMessage {message} />
						</div>
					</div>
				{/if}
			{/each}

			<!-- Error -->
			{#if tab.error}
				<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg flex items-center justify-between shadow-s">
					<span class="text-sm">{tab.error}</span>
					<button onclick={() => tabs.clearTabError(tab.id)} class="text-destructive hover:opacity-80">&times;</button>
				</div>
			{/if}
		</div>
	{/if}
</div>
