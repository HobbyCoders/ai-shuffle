<script lang="ts">
	/**
	 * ThinkingBlock - Collapsible display of Claude's thinking process
	 *
	 * Renders thinking content as italicized, muted text to distinguish
	 * it from regular response content while keeping it readable.
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

	// Start collapsed by default, but expand while streaming
	let isExpanded = $state(false);

	// Auto-expand while streaming for visibility
	$effect(() => {
		if (streaming) {
			isExpanded = true;
		}
	});

	function renderMarkdown(text: string): string {
		try {
			// Strip code blocks - they don't render well with italic styling
			// Remove fenced code blocks (```...```)
			let cleaned = text.replace(/```[\s\S]*?```/g, '');
			// Remove inline code (`...`)
			cleaned = cleaned.replace(/`[^`]+`/g, '');
			return marked.parse(cleaned) as string;
		} catch {
			return text;
		}
	}

	function formatTime(date?: Date): string {
		const d = date || new Date();
		return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
	}

</script>

<div class="thinking-block" class:streaming>
	<!-- Collapsible Header -->
	<button
		onclick={() => isExpanded = !isExpanded}
		class="thinking-header"
	>
		<!-- Brain icon -->
		<svg class="thinking-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
		</svg>

		<span class="thinking-label">Thinking</span>
		<span class="thinking-time">{formatTime(timestamp)}</span>

		{#if streaming}
			<StreamingIndicator />
		{/if}

		<!-- Chevron -->
		<svg
			class="thinking-chevron {isExpanded ? 'expanded' : ''}"
			fill="none"
			stroke="currentColor"
			viewBox="0 0 24 24"
		>
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
		</svg>
	</button>

	<!-- Expandable Content -->
	{#if isExpanded && content}
		<div class="thinking-content prose prose-sm max-w-none">
			{@html renderMarkdown(content)}
		</div>
	{/if}
</div>

<style>
	.thinking-block {
		font-style: italic;
		color: var(--text-muted, hsl(var(--muted-foreground)));
		opacity: 0.85;
		margin: 0.5rem 0;
	}

	.thinking-block.streaming {
		/* Subtle pulse effect while streaming */
	}

	.thinking-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-style: normal;
		width: 100%;
		text-align: left;
		padding: 0.375rem 0.5rem;
		margin: 0 -0.5rem;
		border: none;
		background: transparent;
		border-radius: 0.375rem;
		cursor: pointer;
		transition: background-color 150ms ease;
	}

	.thinking-header:hover {
		background: var(--muted, hsl(var(--muted) / 0.3));
	}

	.thinking-icon {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
		color: #22d3ee; /* cyan-400 */
	}

	.thinking-label {
		font-weight: 600;
		font-size: 0.875rem;
		color: #22d3ee; /* cyan-400 */
	}

	.thinking-time {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		opacity: 0.8;
	}

	.thinking-chevron {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
		margin-left: auto;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.thinking-chevron.expanded {
		transform: rotate(180deg);
	}

	.thinking-content {
		font-size: var(--text-sm, 0.875rem);
		line-height: 1.6;
		padding: 0.5rem 0;
		margin-left: 1.5rem; /* Indent under icon */
	}

	/* Override prose styles to maintain italic + muted appearance */
	.thinking-content :global(p) {
		margin: 0.375rem 0;
		color: inherit;
		font-style: italic;
	}

	.thinking-content :global(p:first-child) {
		margin-top: 0;
	}

	.thinking-content :global(p:last-child) {
		margin-bottom: 0;
	}

	/* Headers in thinking should be subtle */
	.thinking-content :global(h1),
	.thinking-content :global(h2),
	.thinking-content :global(h3),
	.thinking-content :global(h4) {
		color: inherit;
		font-style: italic;
		font-size: 1em;
		font-weight: 600;
		margin: 0.5rem 0 0.25rem 0;
	}

	/* Lists */
	.thinking-content :global(ul),
	.thinking-content :global(ol) {
		padding-left: 1.25rem;
		margin: 0.375rem 0;
	}

	.thinking-content :global(li) {
		margin: 0.125rem 0;
	}
</style>
