<script lang="ts">
	/**
	 * ThinkingBlock - Collapsible component to display Claude's thinking process
	 *
	 * Shows the extended thinking content from Claude in a collapsible block.
	 * Users can expand/collapse to see Claude's reasoning process.
	 */
	import { marked } from 'marked';
	import StreamingIndicator from './StreamingIndicator.svelte';

	// Configure marked for markdown rendering
	marked.setOptions({
		breaks: true,
		gfm: true
	});

	interface Props {
		content: string;
		timestamp?: Date;
		streaming?: boolean;
	}

	let {
		content,
		timestamp,
		streaming = false
	}: Props = $props();

	let isExpanded = $state(false);

	function formatTime(date?: Date): string {
		const d = date || new Date();
		return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
	}

	function renderMarkdown(text: string): string {
		try {
			return marked.parse(text) as string;
		} catch {
			return text;
		}
	}

	// Count approximate lines for preview
	const lineCount = $derived(content ? content.split('\n').length : 0);
	const charCount = $derived(content?.length || 0);
</script>

<div class="thinking-block" class:streaming>
	<!-- Header - always visible, clickable to toggle -->
	<button
		class="thinking-header"
		class:expanded={isExpanded}
		onclick={() => isExpanded = !isExpanded}
		aria-expanded={isExpanded}
	>
		<div class="thinking-icon">
			<svg
				class="icon-brain"
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
				width="16"
				height="16"
			>
				<path
					stroke-linecap="round"
					stroke-linejoin="round"
					stroke-width="2"
					d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"
				/>
			</svg>
		</div>
		<span class="thinking-label">Thinking</span>
		{#if streaming}
			<StreamingIndicator />
		{:else}
			<span class="thinking-meta">{charCount.toLocaleString()} chars</span>
		{/if}
		<span class="thinking-time">{formatTime(timestamp)}</span>
		<svg
			class="chevron"
			class:rotated={isExpanded}
			fill="none"
			stroke="currentColor"
			viewBox="0 0 24 24"
			width="16"
			height="16"
		>
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
		</svg>
	</button>

	<!-- Collapsible content -->
	{#if isExpanded}
		<div class="thinking-content">
			<div class="prose prose-sm max-w-none">
				{#if content}
					{@html renderMarkdown(content)}
				{:else if streaming}
					<div class="flex gap-1 py-2">
						<span class="dot"></span>
						<span class="dot" style="animation-delay: 200ms"></span>
						<span class="dot" style="animation-delay: 400ms"></span>
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.thinking-block {
		margin: 0.5rem 0;
		border-radius: var(--radius-md, 8px);
		border: 1px solid var(--border-subtle, hsl(var(--border) / 0.5));
		background: color-mix(in oklch, var(--primary, hsl(var(--primary))) 5%, var(--surface-0, hsl(var(--background))));
		overflow: hidden;
	}

	.thinking-block.streaming {
		border-color: color-mix(in oklch, var(--primary, hsl(var(--primary))) 30%, transparent);
	}

	.thinking-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		padding: 0.625rem 0.75rem;
		background: transparent;
		border: none;
		cursor: pointer;
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, hsl(var(--muted-foreground)));
		transition: background var(--transition-fast, 100ms);
	}

	.thinking-header:hover {
		background: color-mix(in oklch, var(--primary, hsl(var(--primary))) 8%, transparent);
	}

	.thinking-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: 4px;
		background: color-mix(in oklch, var(--primary, hsl(var(--primary))) 15%, transparent);
		color: var(--primary, hsl(var(--primary)));
	}

	.icon-brain {
		width: 14px;
		height: 14px;
	}

	.thinking-label {
		font-weight: 500;
		color: var(--text-primary, hsl(var(--foreground)));
	}

	.thinking-meta {
		color: var(--text-muted, hsl(var(--muted-foreground)));
		font-size: var(--text-xs, 0.6875rem);
	}

	.thinking-time {
		margin-left: auto;
		color: var(--text-muted, hsl(var(--muted-foreground)));
		font-size: var(--text-xs, 0.6875rem);
	}

	.chevron {
		color: var(--text-muted, hsl(var(--muted-foreground)));
		transition: transform var(--transition-smooth, 200ms);
	}

	.chevron.rotated {
		transform: rotate(180deg);
	}

	.thinking-content {
		padding: 0.75rem 1rem;
		padding-top: 0;
		border-top: 1px solid var(--border-subtle, hsl(var(--border) / 0.3));
		max-height: 400px;
		overflow-y: auto;
	}

	.thinking-content .prose {
		color: var(--text-secondary, hsl(var(--muted-foreground)));
		font-size: var(--text-sm, 0.8125rem);
		line-height: 1.6;
	}

	/* Prose styling for thinking content - slightly muted */
	.thinking-content :global(h1),
	.thinking-content :global(h2),
	.thinking-content :global(h3),
	.thinking-content :global(h4) {
		color: var(--text-primary, hsl(var(--foreground)));
		opacity: 0.9;
	}

	.thinking-content :global(p) {
		margin: 0.5rem 0;
	}

	.thinking-content :global(code) {
		background: var(--surface-1, hsl(var(--muted) / 0.5));
		padding: 0.125rem 0.25rem;
		border-radius: 3px;
		font-size: 0.85em;
	}

	.thinking-content :global(pre) {
		background: var(--surface-1, hsl(var(--muted)));
		padding: 0.75rem;
		border-radius: var(--radius-sm, 6px);
		overflow-x: auto;
		margin: 0.75rem 0;
	}

	.thinking-content :global(pre code) {
		background: transparent;
		padding: 0;
	}

	.thinking-content :global(ul),
	.thinking-content :global(ol) {
		padding-left: 1.25rem;
		margin: 0.5rem 0;
	}

	.thinking-content :global(li) {
		margin: 0.25rem 0;
	}

	/* Scrollbar styling */
	.thinking-content::-webkit-scrollbar {
		width: 6px;
	}

	.thinking-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.thinking-content::-webkit-scrollbar-thumb {
		background-color: var(--border-default, hsl(var(--border)));
		border-radius: 3px;
	}

	.thinking-content::-webkit-scrollbar-thumb:hover {
		background-color: var(--text-tertiary, hsl(var(--muted-foreground)));
	}

	/* Loading dots */
	.dot {
		width: 0.5rem;
		height: 0.5rem;
		background: var(--text-muted, hsl(var(--muted-foreground)));
		border-radius: 50%;
		animation: pulse 1.5s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 0.3; }
		50% { opacity: 1; }
	}
</style>
