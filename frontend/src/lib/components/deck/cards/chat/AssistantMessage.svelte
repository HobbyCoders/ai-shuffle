<script lang="ts">
	/**
	 * AssistantMessage - Display for AI responses with markdown rendering
	 */
	import type { ChatMessage } from '$lib/stores/tabs';
	import { marked } from 'marked';

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	// Configure marked for safe rendering
	marked.setOptions({
		breaks: true,
		gfm: true
	});

	// Render markdown content
	function renderMarkdown(content: string): string {
		if (!content) return '';
		try {
			return marked(content, { breaks: true }) as string;
		} catch {
			return content;
		}
	}
</script>

<div class="flex flex-col gap-1 items-start">
	<!-- Role label -->
	<div class="flex items-center gap-2 px-1">
		<span class="text-xs font-medium text-purple-400">Claude</span>
	</div>

	<!-- Message bubble -->
	<div class="max-w-[90%] rounded-2xl px-4 py-2.5 bg-card border border-border/50 text-foreground rounded-bl-sm">
		{#if message.streaming && !message.content}
			<!-- Streaming placeholder -->
			<div class="flex items-center gap-1">
				<span class="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style="animation-delay: 0ms"></span>
				<span class="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style="animation-delay: 150ms"></span>
				<span class="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style="animation-delay: 300ms"></span>
			</div>
		{:else if message.content}
			<div class="prose prose-sm prose-invert max-w-none assistant-message-content">
				{@html renderMarkdown(message.content)}
			</div>
		{/if}

		<!-- Streaming indicator while typing -->
		{#if message.streaming && message.content}
			<div class="flex items-center gap-1 mt-2 pt-2 border-t border-border/30">
				<div class="flex gap-1">
					<span class="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style="animation-delay: 0ms"></span>
					<span class="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style="animation-delay: 150ms"></span>
					<span class="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style="animation-delay: 300ms"></span>
				</div>
				<span class="text-xs text-muted-foreground ml-2">Typing...</span>
			</div>
		{/if}
	</div>
</div>

<style>
	/* Markdown styling for assistant messages */
	.assistant-message-content :global(p) {
		margin-bottom: 0.5rem;
	}
	.assistant-message-content :global(p:last-child) {
		margin-bottom: 0;
	}
	.assistant-message-content :global(pre) {
		background: var(--muted);
		border-radius: 0.5rem;
		padding: 0.75rem;
		overflow-x: auto;
		margin: 0.5rem 0;
		font-size: 0.75rem;
	}
	.assistant-message-content :global(code) {
		background: var(--muted);
		padding: 0.125rem 0.25rem;
		border-radius: 0.25rem;
		font-size: 0.8em;
	}
	.assistant-message-content :global(pre code) {
		background: transparent;
		padding: 0;
	}
	.assistant-message-content :global(ul),
	.assistant-message-content :global(ol) {
		padding-left: 1.25rem;
		margin: 0.5rem 0;
	}
	.assistant-message-content :global(li) {
		margin: 0.25rem 0;
	}
	.assistant-message-content :global(h1),
	.assistant-message-content :global(h2),
	.assistant-message-content :global(h3),
	.assistant-message-content :global(h4) {
		font-weight: 600;
		margin-top: 0.75rem;
		margin-bottom: 0.25rem;
	}
	.assistant-message-content :global(h1) { font-size: 1.25rem; }
	.assistant-message-content :global(h2) { font-size: 1.125rem; }
	.assistant-message-content :global(h3) { font-size: 1rem; }
	.assistant-message-content :global(h4) { font-size: 0.875rem; }
	.assistant-message-content :global(blockquote) {
		border-left: 3px solid var(--border);
		padding-left: 0.75rem;
		margin: 0.5rem 0;
		color: var(--muted-foreground);
	}
	.assistant-message-content :global(a) {
		color: var(--primary);
		text-decoration: underline;
	}
	.assistant-message-content :global(table) {
		border-collapse: collapse;
		width: 100%;
		margin: 0.5rem 0;
		font-size: 0.75rem;
	}
	.assistant-message-content :global(th),
	.assistant-message-content :global(td) {
		border: 1px solid var(--border);
		padding: 0.25rem 0.5rem;
		text-align: left;
	}
	.assistant-message-content :global(th) {
		background: var(--muted);
	}
</style>
