<script lang="ts">
	/**
	 * ThinkingBlock - Collapsible display of Claude's thinking process
	 *
	 * Renders thinking content as italicized, muted text to distinguish
	 * it from regular response content while keeping it readable.
	 */
	import { marked } from 'marked';
	import { onDestroy } from 'svelte';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { Typewriter } from '$lib/utils/typewriter';
	import StreamingIndicator from './StreamingIndicator.svelte';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';

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
		/** Profile name to display in header (defaults to "Assistant") */
		profileName?: string;
		/** 'reasoning_summary' when this block is the provider's own summary of
		 *  the model's reasoning (OpenAI Responses / xAI), not the reasoning
		 *  itself. The header says so: a summary written in the second person
		 *  read like the model addressing the user. */
		subtype?: string | null;
	}

	let {
		content,
		streaming = false,
		initialExpanded,
		profileName = 'Assistant',
		subtype = null
	}: Props = $props();

	const isSummary = $derived(subtype === 'reasoning_summary');
	const label = $derived(isSummary ? 'Reasoning summary' : 'Thinking');
	const labelTitle = $derived(
		isSummary
			? "The provider's own summary of the model's reasoning, not the reasoning itself"
			: undefined
	);

	// Chat preferences (reactive via .current getter)
	const prefs = $derived(chatPreferences.current);

	// Track if user has manually toggled this block
	// Once toggled, user's choice takes precedence over preference
	let userToggled = $state(false);
	let userExpandedChoice = $state(false);

	// Compute expanded state: user toggle > prop override > preference. The
	// user's click must always win — with the prop first, a supplied
	// initialExpanded made the header a dead control.
	const isExpanded = $derived(
		userToggled
			? userExpandedChoice
			: initialExpanded !== undefined
				? initialExpanded
				: prefs.thinkingStartExpanded
	);

	function toggleExpanded() {
		userToggled = true;
		userExpandedChoice = !isExpanded;
	}

	// Collapsed preview: the actual thinking text, markdown-stripped. The CSS
	// line clamp decides how much fits; this only bounds the string so a huge
	// block doesn't land in the DOM. While streaming the TAIL is the useful
	// part — it shows what is being thought right now.
	function getPreview(text: string, live: boolean): string {
		if (!text) return '';

		const cleaned = text
			.replace(/```[\s\S]*?```/g, ' ')   // Remove code blocks
			.replace(/`[^`]+`/g, ' ')          // Remove inline code
			.replace(/#+\s*/g, '')             // Remove headers
			.replace(/\*\*([^*]+)\*\*/g, '$1') // Bold to plain
			.replace(/\*([^*]+)\*/g, '$1')     // Italic to plain
			.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // Links to text
			.replace(/\s+/g, ' ')              // Whitespace runs to spaces
			.trim();

		const MAX = 400;
		if (cleaned.length <= MAX) return cleaned;

		if (live) {
			const tail = cleaned.slice(-MAX);
			const firstSpace = tail.indexOf(' ');
			return '…' + (firstSpace > 0 && firstSpace < 40 ? tail.slice(firstSpace + 1) : tail);
		}
		const head = cleaned.slice(0, MAX);
		const lastSpace = head.lastIndexOf(' ');
		return (lastSpace > MAX - 40 ? head.slice(0, lastSpace) : head) + '…';
	}

	// ── Typewriter animation ───────────────────────────────────────────────
	// Smooth char-by-char reveal during streaming (same as AssistantMessage).
	let _twLength = $state(0);
	const _tw = new Typewriter((len) => { _twLength = len; });

	$effect(() => { _tw.setTarget(content || '', streaming); });
	onDestroy(() => _tw.destroy());

	const displayContent = $derived(
		streaming && _twLength < (content || '').length
			? (content || '').slice(0, _twLength)
			: (content || '')
	);

	// ── Incremental markdown rendering ──────────────────────────────────────
	// During streaming, thinking content is only appended. Cache the rendered
	// HTML for completed paragraphs and only re-parse the tail.
	// Use a plain object to avoid Svelte 5 compiling these as reactive signals.
	// Mutating reactive `let` vars inside `$derived` corrupts the effect linked list.
	const _cache = { content: '', html: '', stablePrefixContent: '', stablePrefixHtml: '' };

	function renderMarkdownMemoized(text: string): string {
		if (!text) return '';
		if (text === _cache.content) return _cache.html;

		// Incremental: if content was only appended
		if (_cache.content.length > 0 && text.startsWith(_cache.content)) {
			const lastBoundary = text.lastIndexOf('\n\n');
			if (lastBoundary > 0) {
				const stableContent = text.substring(0, lastBoundary);
				const tailContent = text.substring(lastBoundary);

				let prefixHtml: string;
				if (stableContent === _cache.stablePrefixContent) {
					prefixHtml = _cache.stablePrefixHtml;
				} else {
					try { prefixHtml = marked.parse(stableContent) as string; } catch { prefixHtml = stableContent; }
					_cache.stablePrefixContent = stableContent;
					_cache.stablePrefixHtml = prefixHtml;
				}

				let tailHtml: string;
				try { tailHtml = marked.parse(tailContent) as string; } catch { tailHtml = tailContent; }

				_cache.content = text;
				_cache.html = prefixHtml + tailHtml;
				return _cache.html;
			}
		}

		// Full render
		_cache.stablePrefixContent = '';
		_cache.stablePrefixHtml = '';
		_cache.content = text;
		try { _cache.html = marked.parse(text) as string; } catch { _cache.html = text; }
		return _cache.html;
	}

	const renderedHtml = $derived(sanitizeHtml(renderMarkdownMemoized(displayContent)));

	// Derived: should show summary when collapsed
	const showSummary = $derived(!isExpanded && prefs.showThinkingSummary && content);
</script>

<div class="thinking-block" class:streaming>
	<!-- Collapsible Header -->
	<button
		type="button"
		onclick={toggleExpanded}
		class="thinking-header"
		aria-expanded={isExpanded}
	>
		<!-- Brain/thinking icon -->
		<svg class="thinking-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
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

		<span class="thinking-label" title={labelTitle}>{label}</span>
		<span class="thinking-who" title={profileName}>{profileName}</span>

		<span class="thinking-right">
			<!-- Chevron -->
			<svg
				class="thinking-chevron {isExpanded ? 'expanded' : ''}"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
				viewBox="0 0 24 24"
				aria-hidden="true"
			>
				<path d="m6 9 6 6 6-6" />
			</svg>

			{#if streaming}
				<StreamingIndicator />
			{/if}
		</span>
	</button>

	<!-- Preview when collapsed: real thinking text, clamped to three lines -->
	{#if showSummary}
		<div class="thinking-summary">
			{getPreview(displayContent, streaming)}
		</div>
	{/if}

	<!-- Expandable Content -->
	{#if isExpanded && content}
		<div class="thinking-content prose prose-sm max-w-none">
			{@html renderedHtml}
		</div>
	{/if}
</div>

<style>
	.thinking-block {
		font-style: italic;
		color: var(--muted-foreground);
		margin: 0;
		min-width: 0;
	}

	/* .thinking-block.streaming — reserved for future pulse effect */

	/* Same flat-row idiom as the work log: subtle icon, bold short name, muted
	   detail, right cluster. Thinking is a step, not a card. */
	.thinking-header {
		display: flex;
		align-items: center;
		gap: 9px;
		font-style: normal;
		width: 100%;
		text-align: left;
		padding: 6px 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		cursor: pointer;
		transition: background-color 130ms ease;
	}

	.thinking-header:hover {
		background: var(--scrim);
	}

	.thinking-header:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	@media (max-width: 639px) {
		.thinking-header {
			min-height: 44px;
		}
	}

	.thinking-label {
		flex: none;
		font-weight: 600;
		font-size: 12.5px;
		color: var(--foreground);
		white-space: nowrap;
	}

	.thinking-who {
		min-width: 0;
		font-size: 11.5px;
		font-family: var(--font-mono);
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.thinking-icon {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
		color: var(--subtle-foreground);
	}

	.thinking-right {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 8px;
		flex: none;
		color: var(--subtle-foreground);
	}

	.thinking-chevron {
		width: 13px;
		height: 13px;
		flex-shrink: 0;
		color: var(--subtle-foreground);
		transition: transform 160ms ease;
	}

	.thinking-chevron.expanded {
		transform: rotate(180deg);
	}

	.thinking-summary {
		font-size: 12.5px;
		line-height: 1.55;
		margin: 2px 0 0 14px;
		padding: 2px 0 2px 12px;
		color: var(--subtle-foreground);
		font-style: italic;
		border-left: 1px solid var(--border);
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		overflow-wrap: anywhere;
	}

	.thinking-content {
		font-size: 13px;
		line-height: 1.6;
		margin: 4px 0 0 14px;
		padding: 2px 0 2px 12px;
		border-left: 1px solid var(--border);
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
