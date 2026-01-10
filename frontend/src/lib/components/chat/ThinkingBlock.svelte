<script lang="ts">
	/**
	 * ThinkingBlock - Collapsible display of Claude's thinking process
	 *
	 * Renders thinking content as italicized, muted text to distinguish
	 * it from regular response content while keeping it readable.
	 */
	import { marked } from 'marked';
	import StreamingIndicator from './StreamingIndicator.svelte';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences';

	// Configure marked for markdown rendering
	marked.setOptions({
		breaks: true,
		gfm: true
	});

	interface Props {
		content: string;
		streaming?: boolean;
		/** Override the default expand state (useful for restoring scroll position) */
		initialExpanded?: boolean;
	}

	let {
		content,
		streaming = false,
		initialExpanded
	}: Props = $props();

	// Use prop override if provided, otherwise use preference
	let isExpanded = $state(initialExpanded ?? chatPreferences.getValue('thinkingStartExpanded'));

	// Subscribe to preferences for reactive updates
	let prefs = $state<ChatPreferences | null>(null);
	$effect(() => {
		const unsubscribe = chatPreferences.subscribe(p => {
			prefs = p;
		});
		return unsubscribe;
	});

	// Generate a summary of the thinking content (first ~100 chars of meaningful text)
	function getSummary(text: string): string {
		if (!text) return '';

		// Strip markdown formatting for cleaner summary
		let cleaned = text
			.replace(/```[\s\S]*?```/g, '') // Remove code blocks
			.replace(/`[^`]+`/g, '')        // Remove inline code
			.replace(/#+\s*/g, '')          // Remove headers
			.replace(/\*\*([^*]+)\*\*/g, '$1') // Bold to plain
			.replace(/\*([^*]+)\*/g, '$1')    // Italic to plain
			.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // Links to text
			.replace(/\n+/g, ' ')            // Newlines to spaces
			.trim();

		// Get first ~120 chars, break at word boundary
		if (cleaned.length <= 120) return cleaned;

		const truncated = cleaned.substring(0, 120);
		const lastSpace = truncated.lastIndexOf(' ');
		return (lastSpace > 80 ? truncated.substring(0, lastSpace) : truncated) + '…';
	}

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

	// Derived: should show summary when collapsed
	const showSummary = $derived(!isExpanded && prefs?.showThinkingSummary && content);
</script>

<div class="thinking-block" class:streaming>
	<!-- Collapsible Header -->
	<button
		onclick={() => isExpanded = !isExpanded}
		class="thinking-header"
	>
		<span class="thinking-label">Assistant</span>
		<!-- Brain/thinking icon -->
		<svg class="thinking-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
			<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
			<path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
			<path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/>
			<path d="M17.599 6.5a3 3 0 0 0 .399-1.375"/>
			<path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/>
			<path d="M3.477 10.896a4 4 0 0 1 .585-.396"/>
			<path d="M19.938 10.5a4 4 0 0 1 .585.396"/>
			<path d="M6 18a4 4 0 0 1-1.967-.516"/>
			<path d="M19.967 17.484A4 4 0 0 1 18 18"/>
		</svg>

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

	<!-- Summary when collapsed -->
	{#if showSummary}
		<div class="thinking-summary">
			{getSummary(content)}
		</div>
	{/if}

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

	.thinking-label {
		font-weight: 600;
		font-size: 0.875rem;
		color: #fb923c; /* orange-400 - matches assistant message header */
	}

	.thinking-icon {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
		color: var(--muted-foreground);
		opacity: 0.7;
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

	.thinking-summary {
		font-size: 0.8125rem;
		line-height: 1.5;
		padding: 0.25rem 0.5rem;
		margin: 0 -0.5rem;
		color: var(--muted-foreground);
		opacity: 0.7;
		font-style: italic;
		border-left: 2px solid var(--border);
		margin-left: 0;
		padding-left: 0.75rem;
	}

	.thinking-content {
		font-size: var(--text-sm, 0.875rem);
		line-height: 1.6;
		padding: 0.5rem 0;
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
