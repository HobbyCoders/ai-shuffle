<script lang="ts">
	/**
	 * ChatCard - Chat conversation card for The Deck
	 *
	 * Connects directly to the tabs store for:
	 * - Real-time WebSocket streaming
	 * - Message rendering with proper styling
	 * - Send/stop functionality
	 * - Session management
	 */
	import BaseCard from './BaseCard.svelte';
	import { MessageList, ChatInput } from './chat';
	import type { ChatMessage } from '$lib/stores/tabs';

	interface Props {
		id: string;
		title: string;
		sessionId: string;
		messages?: ChatMessage[];
		isStreaming?: boolean;
		profile?: string;
		project?: string;
		pinned?: boolean;
		minimized?: boolean;
		active?: boolean;
		onpin?: () => void;
		onminimize?: () => void;
		onclose?: () => void;
		onactivate?: () => void;
		onsendmessage?: (message: string) => void;
		onstopstreaming?: () => void;
		onclear?: () => void;
		onexport?: () => void;
		onfork?: () => void;
	}

	let {
		id,
		title,
		sessionId,
		messages = [],
		isStreaming = false,
		profile,
		project,
		pinned = false,
		minimized = false,
		active = false,
		onpin,
		onminimize,
		onclose,
		onactivate,
		onsendmessage,
		onstopstreaming,
		onclear,
		onexport,
		onfork
	}: Props = $props();

	// Reference to ChatInput for focus management
	let chatInputRef: { focus: () => void } | undefined = $state();

	// Focus input when card becomes active
	$effect(() => {
		if (active && chatInputRef && !minimized) {
			// Small delay to ensure DOM is ready
			setTimeout(() => {
				chatInputRef?.focus();
			}, 50);
		}
	});

	function handleSendMessage(message: string) {
		onsendmessage?.(message);
	}

	function handleStopStreaming() {
		onstopstreaming?.();
	}

	function handleClear() {
		onclear?.();
	}

	function handleExport() {
		onexport?.();
	}

	function handleFork() {
		onfork?.();
	}
</script>

<BaseCard
	{id}
	{title}
	type="chat"
	{pinned}
	{minimized}
	{active}
	{onpin}
	{onminimize}
	{onclose}
	{onactivate}
>
	{#snippet headerActions()}
		<!-- Profile badge -->
		{#if profile}
			<span class="text-xs px-2 py-0.5 rounded-full bg-primary/10 text-primary">
				{profile}
			</span>
		{/if}
		<!-- Project badge -->
		{#if project}
			<span class="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
				{project}
			</span>
		{/if}
		<!-- Streaming indicator -->
		{#if isStreaming}
			<span class="flex items-center gap-1 text-xs text-primary">
				<span class="w-1.5 h-1.5 rounded-full bg-primary animate-pulse"></span>
				Live
			</span>
		{/if}
	{/snippet}

	<!-- Message list with auto-scroll -->
	<MessageList {messages} {isStreaming} />

	{#snippet footer()}
		<ChatInput
			bind:this={chatInputRef}
			{isStreaming}
			placeholder={isStreaming ? 'Waiting for response...' : 'Type a message...'}
			onsend={handleSendMessage}
			onstop={handleStopStreaming}
			onclear={handleClear}
			onexport={handleExport}
			onfork={handleFork}
			messageCount={messages.length}
		/>

		<!-- Session info footer -->
		<div class="flex items-center justify-between px-4 pb-2 text-xs text-muted-foreground/60">
			<span>{messages.length} message{messages.length !== 1 ? 's' : ''}</span>
			{#if sessionId}
				<span class="font-mono">{sessionId.slice(0, 8)}</span>
			{/if}
		</div>
	{/snippet}
</BaseCard>
