<script lang="ts">
	/**
	 * AssistantMessage - Assistant message with markdown rendering and hover-only action buttons
	 */
	import { onDestroy } from 'svelte';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { rewriteMediaMarkdown, clampRevealToMediaBoundary } from '$lib/utils/media';
	import { renderChatMarkdown } from '$lib/utils/chat-markdown';
	import { Typewriter } from '$lib/utils/typewriter';
	import MessageActions from './MessageActions.svelte';
	import { formatMessageTime } from '$lib/utils/message-time';

	interface Props {
		content: string;
		/** Real-time WS stream buffer — updates every delta, not just every poll */
		wsContent?: string;
		timestamp?: Date | string;
		messageIndex: number;
		messageId: string;
		messageUuid?: string | null;  // UUID from JSONL for direct rewind (may be same as messageId)
		sessionId?: string | null;
		streaming?: boolean;
		hasPartialMessages?: boolean;
		fileCheckpointingEnabled?: boolean;  // If true, show option to rewind files too
		metadata?: Record<string, unknown>;
		onCopy?: (content: string, messageId: string) => void;
		onRewindComplete?: () => void;
		/** Profile name to display in header (defaults to "Assistant") */
		profileName?: string;
		/** Model display name for this specific message, when the turn reported one. */
		modelLabel?: string | null;
	}

	let {
		content,
		wsContent = '',
		timestamp,
		messageIndex,
		messageId,
		messageUuid = null,  // UUID from JSONL for direct rewind
		sessionId,
		streaming = false,
		hasPartialMessages = true,
		fileCheckpointingEnabled = false,
		metadata,
		onCopy,
		onRewindComplete,
		profileName = 'Assistant',
		modelLabel = null
	}: Props = $props();

	/** The name line's text: the model that produced this message when we know
	 *  it, otherwise the profile that was active. */
	const speakerName = $derived(modelLabel || profileName);

	// Determine if this is a final message with cost/duration metadata (query completion)
	const hasFinalMetadata = $derived(Boolean(
		metadata && (metadata.total_cost_usd || metadata.cost_usd || metadata.duration_ms)
	));

	// Show Read button only on final messages with content
	const canRead = $derived(Boolean(!streaming && content && content.length > 0 && hasFinalMetadata));

	let isHovered = $state(false);
	let isTouchDevice = $state(false);
	let showActionsOnMobile = $state(false);
	let isReading = $state(false);
	let currentAudio = $state<HTMLAudioElement | null>(null);

	function handleTouchStart() {
		isTouchDevice = true;
	}

	function handleMessageClick(e: MouseEvent) {
		// Don't toggle if clicking on interactive elements (links, buttons, code blocks, etc.)
		const target = e.target as HTMLElement;
		if (target.closest('a, button, pre, code, video, .media-overlay-buttons')) {
			return;
		}
		// Only toggle on touch devices - desktop uses hover
		if (isTouchDevice) {
			showActionsOnMobile = !showActionsOnMobile;
		}
	}

	// Show actions if hovered (desktop) or tapped (mobile)
	const showActions = $derived(isHovered || showActionsOnMobile);

	function formatCost(cost: number | undefined): string {
		if (cost === undefined) return '';
		return `$${cost.toFixed(4)}`;
	}

	// ── Typewriter animation ───────────────────────────────────────────────
	// Smoothly reveals streaming content char-by-char via rAF instead of
	// showing chunky 150ms poll bursts. Purely presentational — does not
	// modify the data pipeline, WS buffer, or single-writer pattern.
	//
	// Uses wsContent (WS buffer, updates every ~50-100ms) when available
	// and longer than poller content, giving sub-poll-interval granularity.
	let _twLength = $state(0);
	const _tw = new Typewriter((len) => { _twLength = len; });

	/** Best available content: WS buffer if ahead, otherwise poller content */
	const _twBestContent = $derived(
		streaming && wsContent && wsContent.length > (content || '').length
			? wsContent
			: (content || '')
	);

	$effect(() => { _tw.setTarget(_twBestContent, streaming); });

	/** Content fed to the markdown renderer — smoothed during streaming. The
	 * reveal length is clamped so a media markdown token is never shown
	 * half-typed; it pops in whole once the cursor passes its end. */
	const displayContent = $derived(
		streaming && _twLength < _twBestContent.length
			? _twBestContent.slice(0, clampRevealToMediaBoundary(_twBestContent, _twLength))
			: _twBestContent
	);

	// ── Incremental markdown rendering ──────────────────────────────────────
	// During streaming, content is only appended. We cache the rendered HTML
	// for completed paragraph blocks and only re-parse the tail, reducing
	// marked() work from O(full_content) to O(last_paragraph) per update.

	// Streaming cursor HTML to inject inline with text
	const STREAMING_CURSOR = '<span class="streaming-cursor inline-block w-2 h-4 ml-0.5 bg-primary animate-pulse align-middle"></span>';

	// Use a plain object to avoid Svelte 5 compiling these as reactive signals.
	// Mutating reactive `let` vars inside `$derived` corrupts the effect linked list.
	// `result.{prefix,tail}` and `stablePrefixHtml` hold SANITIZED markup.
	// Sanitization is folded into the render functions so that during streaming
	// DOMPurify only runs over the growing tail paragraph (and the stable prefix
	// once, when it advances) instead of re-sanitizing the entire message on
	// every delta — which was O(full content) per frame and got heavier as a
	// response grew.
	const _cache = {
		content: '',
		wasStreaming: false,
		stablePrefixContent: '',
		stablePrefixHtml: '',
		result: { prefix: '', tail: '' }
	};

	// Rendered output is split into a stable PREFIX (completed paragraphs, whose
	// HTML string is byte-identical across deltas) and a TAIL (the still-growing
	// last paragraph). The template renders them as two separate {@html} blocks;
	// because {@html} only mutates the DOM when its string value changes, any
	// media that has settled into the prefix is NOT torn down and refetched when
	// the tail keeps streaming. That is what stops the image from flickering /
	// reloading while the rest of the message arrives.
	const rendered = $derived(renderMarkdownMemoized(displayContent, streaming));

	function renderMarkdownMemoized(text: string, isStreaming: boolean): { prefix: string; tail: string } {
		// Fast path: content and streaming flag unchanged
		if (text === _cache.content && isStreaming === _cache.wasStreaming) return _cache.result;

		// Transition from streaming→not-streaming: full re-render for final media processing
		if (_cache.wasStreaming && !isStreaming) {
			_cache.stablePrefixContent = '';
			_cache.stablePrefixHtml = '';
			_cache.content = text;
			_cache.wasStreaming = false;
			_cache.result = { prefix: renderMarkdownFull(text, false), tail: '' };
			return _cache.result;
		}

		// During streaming: incremental render if content was only appended
		if (isStreaming && _cache.content.length > 0 && text.startsWith(_cache.content)) {
			_cache.result = renderMarkdownIncremental(text);
			_cache.content = text;
			_cache.wasStreaming = true;
			return _cache.result;
		}

		// Full render (first render, content rewritten, or non-streaming).
		// While streaming with no cached prefix yet, the whole thing is the tail
		// (carries the streaming cursor); otherwise it is one settled block.
		_cache.stablePrefixContent = '';
		_cache.stablePrefixHtml = '';
		_cache.content = text;
		_cache.wasStreaming = isStreaming;
		_cache.result = isStreaming
			? { prefix: '', tail: renderMarkdownFull(text, true) }
			: { prefix: renderMarkdownFull(text, false), tail: '' };
		return _cache.result;
	}

	/**
	 * Incremental render: reuse cached HTML for completed paragraphs,
	 * only re-parse from the last paragraph boundary onwards. Returns the
	 * stable prefix and the growing tail separately so the template can render
	 * them as independent {@html} blocks (settled media stays put).
	 */
	function renderMarkdownIncremental(text: string): { prefix: string; tail: string } {
		// Find the last paragraph boundary (\n\n) in the text.
		// Everything before it is "stable" — its markdown structure is complete.
		const lastBoundary = text.lastIndexOf('\n\n');

		if (lastBoundary <= 0) {
			// No paragraph boundary yet — full parse (content is short)
			const html = renderChatMarkdown(rewriteMediaMarkdown(text), false);
			return { prefix: '', tail: sanitizeHtml(injectStreamingCursor(html)) };
		}

		const stableContent = text.substring(0, lastBoundary);
		const tailContent = text.substring(lastBoundary);

		// If the stable prefix hasn't changed, reuse its cached (sanitized) HTML
		let prefixHtml: string;
		if (stableContent === _cache.stablePrefixContent) {
			prefixHtml = _cache.stablePrefixHtml;
		} else {
			// Stable prefix grew — re-parse, sanitize, and cache it once. Only this
			// side gets syntax highlighting: its fences are closed, and Shiki's
			// memo makes the repeat renders cheap.
			prefixHtml = sanitizeHtml(renderChatMarkdown(rewriteMediaMarkdown(stableContent), true));
			_cache.stablePrefixContent = stableContent;
			_cache.stablePrefixHtml = prefixHtml;
		}

		// Always re-parse the tail (last incomplete paragraph + new content).
		// This is the only DOMPurify pass that runs every delta — bounded to the
		// last paragraph rather than the whole message. Highlighting is off here:
		// the fence may not be closed yet, and tokenising per delta would be the
		// most expensive thing in the stream loop.
		let tailHtml = renderChatMarkdown(rewriteMediaMarkdown(tailContent), false);
		tailHtml = sanitizeHtml(injectStreamingCursor(tailHtml));

		return { prefix: prefixHtml, tail: tailHtml };
	}

	/**
	 * Inject the streaming cursor inside the last text-containing element
	 * so it appears inline with the text instead of below block elements.
	 */
	function injectStreamingCursor(html: string): string {
		const inlineClosingTags = ['</p>', '</li>', '</td>', '</th>', '</span>', '</strong>', '</em>', '</a>', '</code>'];

		let lastIndex = -1;
		for (const tag of inlineClosingTags) {
			const idx = html.lastIndexOf(tag);
			if (idx > lastIndex) {
				lastIndex = idx;
			}
		}

		if (lastIndex !== -1) {
			return html.slice(0, lastIndex) + STREAMING_CURSOR + html.slice(lastIndex);
		}
		return html + STREAMING_CURSOR;
	}

	/**
	 * Full markdown render (non-streaming or first render). Media markdown
	 * (images / video / audio) is rewritten into inline players with download +
	 * expand controls in both cases, so generated media shows up immediately —
	 * the only difference while streaming is the trailing cursor.
	 * See $lib/utils/media for detection rules and produced markup.
	 */
	function renderMarkdownFull(text: string, isStreaming: boolean): string {
		const html = renderChatMarkdown(rewriteMediaMarkdown(text), !isStreaming);
		return isStreaming ? sanitizeHtml(injectStreamingCursor(html)) : sanitizeHtml(html);
	}

	// ── Code-block copy ────────────────────────────────────────────────────
	// Delegated rather than inline: the markup comes out of {@html}, so there is
	// no component instance to bind to, and an inline onclick would have to
	// survive DOMPurify. Reading the code back out of the rendered <pre> also
	// means the button always copies exactly what is on screen.
	const _copyTimers = new Set<ReturnType<typeof setTimeout>>();

	function handleProseClick(e: MouseEvent) {
		const target = e.target as HTMLElement | null;
		const btn = target?.closest?.('.code-copy-btn') as HTMLElement | null;
		if (!btn) return;
		e.preventDefault();
		e.stopPropagation();
		const code = btn.closest('.codeblock')?.querySelector('pre code')?.textContent ?? '';
		if (!code) return;
		void navigator.clipboard?.writeText(code).catch(() => {});
		btn.classList.add('is-copied');
		const timer = setTimeout(() => {
			btn.classList.remove('is-copied');
			_copyTimers.delete(timer);
		}, 1200);
		_copyTimers.add(timer);
	}

	/**
	 * Strip markdown/HTML and prepare text for TTS
	 */
	function prepareTextForTTS(text: string): string {
		// Remove code blocks
		let cleaned = text.replace(/```[\s\S]*?```/g, ' code block ');
		// Remove inline code
		cleaned = cleaned.replace(/`[^`]+`/g, ' code ');
		// Remove image markdown
		cleaned = cleaned.replace(/!\[[^\]]*\]\([^)]+\)/g, '');
		// Remove link markdown but keep text
		cleaned = cleaned.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
		// Remove bold markers (** or __) but keep text - use non-greedy match
		cleaned = cleaned.replace(/\*\*(.*?)\*\*/g, '$1');
		cleaned = cleaned.replace(/__(.*?)__/g, '$1');
		// Remove italic markers (* or _) but keep text - use non-greedy match
		cleaned = cleaned.replace(/\*(.*?)\*/g, '$1');
		cleaned = cleaned.replace(/_(.*?)_/g, '$1');
		// Remove headers
		cleaned = cleaned.replace(/^#{1,6}\s+/gm, '');
		// Remove horizontal rules
		cleaned = cleaned.replace(/^[-*_]{3,}$/gm, '');
		// Remove numbered list markers (e.g., "1. ", "2. ", "10. ")
		cleaned = cleaned.replace(/^\d+\.\s+/gm, '');
		// Remove bullet list markers (e.g., "- ", "* ", "+ ")
		cleaned = cleaned.replace(/^[-*+]\s+/gm, '');
		// Remove blockquotes
		cleaned = cleaned.replace(/^>\s*/gm, '');
		// Remove table formatting (pipes and alignment markers)
		cleaned = cleaned.replace(/\|/g, ' ');
		cleaned = cleaned.replace(/^[\s\-:]+$/gm, '');

		// Remove emojis (surrogate pairs for emoji ranges)
		cleaned = cleaned.replace(/[\uD83C-\uDBFF][\uDC00-\uDFFF]/g, '');

		// Remove specific unicode symbol ranges that cause TTS issues:
		// - Misc symbols (U+2600-U+26FF) - weather, zodiac, chess, etc.
		// - Dingbats (U+2700-U+27BF) - ornaments, arrows, etc.
		// - Arrows (U+2190-U+21FF)
		// - Mathematical operators (U+2200-U+22FF)
		// - Box drawing (U+2500-U+257F)
		// - Geometric shapes (U+25A0-U+25FF)
		// - Variation selectors (U+FE00-U+FE0F)
		// - Zero-width characters (U+200B-U+200D, U+FEFF)
		cleaned = cleaned.replace(/[\u2600-\u26FF\u2700-\u27BF\u2190-\u21FF\u2200-\u22FF\u2500-\u257F\u25A0-\u25FF\uFE00-\uFE0F\u200B-\u200D\uFEFF]/g, '');

		// Remove common decorative symbols that might appear in text
		cleaned = cleaned.replace(/[•◦▪▫●○◆◇★☆✓✔✗✘✕✖]/g, '');

		// Clean up extra whitespace
		cleaned = cleaned.replace(/\s+/g, ' ').trim();
		// Limit length for TTS (4000 chars max)
		if (cleaned.length > 4000) {
			cleaned = cleaned.substring(0, 4000) + '...';
		}
		return cleaned;
	}

	/**
	 * Handle reading the message with TTS
	 */
	async function handleRead() {
		// If already reading, stop
		if (isReading && currentAudio) {
			currentAudio.pause();
			currentAudio.currentTime = 0;
			currentAudio = null;
			isReading = false;
			return;
		}

		const ttsText = prepareTextForTTS(content);
		if (!ttsText) return;

		isReading = true;
		let audioUrl: string | null = null;

		try {
			const response = await fetch('/api/v1/settings/tts/stream', {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					'Authorization': `Bearer ${localStorage.getItem('auth_token') || ''}`
				},
				body: JSON.stringify({
					text: ttsText,
					output_format: 'mp3'
				})
			});

			if (!response.ok) {
				throw new Error(`TTS request failed: ${response.status}`);
			}

			const audioBlob = await response.blob();
			audioUrl = URL.createObjectURL(audioBlob);

			const audio = new Audio(audioUrl);
			currentAudio = audio;

			audio.onended = () => {
				isReading = false;
				currentAudio = null;
				if (audioUrl) URL.revokeObjectURL(audioUrl);
			};

			audio.onerror = () => {
				console.error('Audio playback error');
				isReading = false;
				currentAudio = null;
				if (audioUrl) URL.revokeObjectURL(audioUrl);
			};

			await audio.play();
		} catch (e) {
			console.error('Failed to generate TTS:', e);
			isReading = false;
			currentAudio = null;
			if (audioUrl) URL.revokeObjectURL(audioUrl);
		}
	}

	// Cleanup on component destroy
	onDestroy(() => {
		if (currentAudio) {
			currentAudio.pause();
			currentAudio = null;
		}
		for (const timer of _copyTimers) clearTimeout(timer);
		_copyTimers.clear();
		_tw.destroy();
	});
</script>

<div
	class="w-full overflow-hidden message-container"
	onmouseenter={() => isHovered = true}
	onmouseleave={() => { isHovered = false; showActionsOnMobile = false; }}
	ontouchstart={handleTouchStart}
	ontouchend={(e) => { const target = e.target; if (target instanceof HTMLElement && !target.closest('a, button, pre, code, video, .media-overlay-buttons')) { showActionsOnMobile = !showActionsOnMobile; } }}
	role="article"
>
	<div class="min-w-0 max-w-full">
		<div class="aname" title={formatMessageTime(timestamp)}>
			{#if streaming}
				<span class="live" aria-label="Streaming"></span>
			{/if}
			<span class="aname-text">{speakerName}</span>
		</div>
		<div class="overflow-hidden max-w-full">
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="prose prose-sm abody max-w-none break-words"
				style="overflow-wrap: anywhere; word-break: break-word;"
				onclick={handleProseClick}
			>
				{#if content}
					<!-- Two blocks: stable prefix (settled media stays mounted) + streaming tail -->
					{@html rendered.prefix}{@html rendered.tail}
				{:else if streaming && !hasPartialMessages}
					<!-- Only show placeholder dots when partial messages is disabled -->
					<div class="flex gap-1 py-2">
						<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse"></span>
						<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse" style="animation-delay: 200ms"></span>
						<span class="w-2 h-2 bg-muted-foreground rounded-full animate-pulse" style="animation-delay: 400ms"></span>
					</div>
				{/if}
			</div>
		</div>
		{#if metadata && !streaming}
			<div class="ameta">
				{#if timestamp}<span>{formatMessageTime(timestamp)}</span>{/if}
				{#if metadata.total_cost_usd}
					<span>{formatCost(metadata.total_cost_usd as number)}</span>
				{:else if metadata.cost_usd}
					<span>{formatCost(metadata.cost_usd as number)}</span>
				{/if}
				{#if metadata.duration_ms}
					<span>{((metadata.duration_ms as number) / 1000).toFixed(1)}s</span>
				{/if}
				{#if metadata.tokens_out && metadata.duration_ms && (metadata.duration_ms as number) > 0}
					<span>{((metadata.tokens_out as number) / ((metadata.duration_ms as number) / 1000)).toFixed(1)} t/s</span>
				{/if}
				{#if content && hasFinalMetadata}
					<div class="assistant-actions" class:is-visible={showActions}>
						<MessageActions
							{sessionId}
							messageContent={content}
							messageRole="assistant"
							{messageUuid}
							{canRead}
							isStreaming={streaming}
							onRead={handleRead}
							{isReading}
							{onRewindComplete}
							{fileCheckpointingEnabled}
						/>
					</div>
				{/if}
			</div>
		{/if}
	</div>
</div>

<style>
	.message-container {
		position: relative;
	}

	/* ── Name line ─────────────────────────────────────────────────────── */
	.aname {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 11.5px;
		font-weight: 500;
		letter-spacing: 0.02em;
		color: var(--subtle-foreground);
		margin-bottom: 9px;
		min-height: 16px;
	}

	.aname-text {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.live {
		width: 5px;
		height: 5px;
		flex: none;
		border-radius: 50%;
		background: var(--primary);
		box-shadow: 0 0 8px 0 var(--glow);
		animation: aname-pulse 1.4s ease-in-out infinite;
	}

	@keyframes aname-pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.35; }
	}

	@media (prefers-reduced-motion: reduce) {
		.live { animation: none; }
	}

	.ameta {
		margin-top: 10px;
		display: flex;
		align-items: center;
		gap: 12px;
		font-size: 11px;
		font-family: var(--font-mono);
		color: var(--subtle-foreground);
	}

	.assistant-actions {
		margin-left: auto;
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.12s ease;
	}

	.assistant-actions.is-visible {
		opacity: 1;
		pointer-events: auto;
	}

	@media (max-width: 639px) {
		.ameta {
			min-height: 36px;
			gap: 9px;
		}

		.assistant-actions {
			opacity: 1;
			pointer-events: auto;
		}
	}

	/* ── Code block chrome (markup produced by $lib/utils/chat-markdown) ── */
	.abody :global(.codeblock) {
		margin: 0 0 14px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--code-bg);
		overflow: hidden;
	}

	.abody :global(.codehead) {
		display: flex;
		align-items: center;
		gap: 7px;
		padding: 6px 8px 6px 11px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
	}

	.abody :global(.codehead .cb-icon) {
		color: var(--subtle-foreground);
		flex: none;
	}

	.abody :global(.codehead .fname) {
		font-family: var(--font-mono);
		font-size: 11.5px;
		color: var(--muted-foreground);
		margin-right: auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.abody :global(.codehead .lang) {
		font-size: 10px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--subtle-foreground);
		padding: 2px 6px;
		border-radius: 5px;
		border: 1px solid var(--border);
		flex: none;
	}

	.abody :global(.code-copy-btn) {
		width: 24px;
		height: 24px;
		flex: none;
		border: none;
		background: transparent;
		border-radius: 6px;
		display: grid;
		place-items: center;
		cursor: pointer;
		color: var(--subtle-foreground);
		transition: background-color 0.13s ease, color 0.13s ease;
	}

	.abody :global(.code-copy-btn:hover) {
		background: var(--accent);
		color: var(--foreground);
	}

	.abody :global(.code-copy-btn .cb-check) {
		display: none;
	}

	.abody :global(.code-copy-btn.is-copied) {
		color: var(--emerald);
	}

	.abody :global(.code-copy-btn.is-copied .cb-copy) {
		display: none;
	}

	.abody :global(.code-copy-btn.is-copied .cb-check) {
		display: block;
	}

	.abody :global(.codeblock .cb-pre) {
		margin: 0;
		padding: 11px 13px 13px;
		overflow-x: auto;
		background: transparent;
		border-radius: 0;
		font-family: var(--font-mono);
		font-size: 12.25px;
		line-height: 1.72;
		color: var(--muted-foreground);
	}

	.abody :global(.codeblock .cb-pre code) {
		font-size: inherit;
		line-height: inherit;
		background: transparent;
		padding: 0;
		border: none;
	}

	/* Shiki emits both palettes as custom properties on every span, so one
	   highlighted string serves both themes and a theme switch never
	   invalidates the highlight cache. */
	.abody :global(.codeblock .cb-pre span) {
		color: var(--shiki-light, inherit);
	}

	:global(html.dark) .abody :global(.codeblock .cb-pre span) {
		color: var(--shiki-dark, inherit);
	}
</style>
