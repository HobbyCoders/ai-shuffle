<script lang="ts">
	/**
	 * AssistantMessage - Assistant message with markdown rendering and hover-only action buttons
	 */
	import { marked } from 'marked';
	import { onDestroy } from 'svelte';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import StreamingIndicator from './StreamingIndicator.svelte';
	import MessageActions from './MessageActions.svelte';

	// Configure marked for markdown rendering
	marked.setOptions({
		breaks: true,
		gfm: true
	});

	interface Props {
		content: string;
		timestamp?: Date;
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
	}

	let {
		content,
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
		profileName = 'Assistant'
	}: Props = $props();

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

	function formatTime(date?: Date): string {
		const d = date || new Date();
		return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
	}

	function formatCost(cost: number | undefined): string {
		if (cost === undefined) return '';
		return `$${cost.toFixed(4)}`;
	}

	// ── Incremental markdown rendering ──────────────────────────────────────
	// During streaming, content is only appended. We cache the rendered HTML
	// for completed paragraph blocks and only re-parse the tail, reducing
	// marked() work from O(full_content) to O(last_paragraph) per update.

	// Streaming cursor HTML to inject inline with text
	const STREAMING_CURSOR = '<span class="streaming-cursor inline-block w-2 h-4 ml-0.5 bg-primary animate-pulse align-middle"></span>';

	// Use a plain object to avoid Svelte 5 compiling these as reactive signals.
	// Mutating reactive `let` vars inside `$derived` corrupts the effect linked list.
	const _cache = { content: '', html: '', wasStreaming: false, stablePrefixContent: '', stablePrefixHtml: '' };

	const renderedHtml = $derived(sanitizeHtml(renderMarkdownMemoized(content, streaming)));

	function renderMarkdownMemoized(text: string, isStreaming: boolean): string {
		// Fast path: content and streaming flag unchanged
		if (text === _cache.content && isStreaming === _cache.wasStreaming) return _cache.html;

		// Transition from streaming→not-streaming: full re-render for final media processing
		if (_cache.wasStreaming && !isStreaming) {
			_cache.stablePrefixContent = '';
			_cache.stablePrefixHtml = '';
			_cache.content = text;
			_cache.wasStreaming = false;
			_cache.html = renderMarkdownFull(text, false);
			return _cache.html;
		}

		// During streaming: incremental render if content was only appended
		if (isStreaming && _cache.content.length > 0 && text.startsWith(_cache.content)) {
			const result = renderMarkdownIncremental(text);
			_cache.content = text;
			_cache.wasStreaming = true;
			_cache.html = result;
			return result;
		}

		// Full render (first render, content rewritten, or non-streaming)
		_cache.stablePrefixContent = '';
		_cache.stablePrefixHtml = '';
		_cache.content = text;
		_cache.wasStreaming = isStreaming;
		_cache.html = renderMarkdownFull(text, isStreaming);
		return _cache.html;
	}

	/**
	 * Incremental render: reuse cached HTML for completed paragraphs,
	 * only re-parse from the last paragraph boundary onwards.
	 */
	function renderMarkdownIncremental(text: string): string {
		// Find the last paragraph boundary (\n\n) in the text.
		// Everything before it is "stable" — its markdown structure is complete.
		const lastBoundary = text.lastIndexOf('\n\n');

		if (lastBoundary <= 0) {
			// No paragraph boundary yet — full parse (content is short)
			let html = marked(replaceMediaWithPlaceholders(text), { breaks: true }) as string;
			return injectStreamingCursor(html);
		}

		const stableContent = text.substring(0, lastBoundary);
		const tailContent = text.substring(lastBoundary);

		// If the stable prefix hasn't changed, reuse its cached HTML
		let prefixHtml: string;
		if (stableContent === _cache.stablePrefixContent) {
			prefixHtml = _cache.stablePrefixHtml;
		} else {
			// Stable prefix grew — re-parse it and cache
			prefixHtml = marked(replaceMediaWithPlaceholders(stableContent), { breaks: true }) as string;
			_cache.stablePrefixContent = stableContent;
			_cache.stablePrefixHtml = prefixHtml;
		}

		// Always re-parse the tail (last incomplete paragraph + new content)
		let tailHtml = marked(replaceMediaWithPlaceholders(tailContent), { breaks: true }) as string;
		tailHtml = injectStreamingCursor(tailHtml);

		return prefixHtml + tailHtml;
	}

	/**
	 * Replace media markdown with placeholders during streaming to prevent flickering.
	 */
	function replaceMediaWithPlaceholders(text: string): string {
		const placeholder = '<div class="media-placeholder my-4 p-4 bg-muted/30 rounded-lg border border-border text-center text-muted-foreground text-sm">';
		let result = text;
		result = result.replace(/!\[([^\]]*)\]\((data:image\/[^;]+;base64,[^)]+)\)/g, placeholder + 'Image loading...</div>');
		result = result.replace(/!\[([^\]]*)\]\((\/api\/generated-images\/[^)]+)\)/g, placeholder + 'Image loading...</div>');
		result = result.replace(/\[([^\]]*)\]\((\/api\/generated-videos\/[^)]+)\)/g, placeholder + 'Video loading...</div>');
		result = result.replace(/"image_base64"\s*:\s*"([A-Za-z0-9+/=]{100,})"/g, placeholder + 'Image loading...</div>');
		result = result.replace(/[^\]]+\]\((\/api\/files\/[^)]+)\)/g, placeholder + 'File preparing...</div>');
		return result;
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
	 * Full markdown render (non-streaming or first render).
	 */
	function renderMarkdownFull(text: string, isStreaming: boolean): string {
		if (isStreaming) {
			let html = marked(replaceMediaWithPlaceholders(text), { breaks: true }) as string;
			return injectStreamingCursor(html);
		}

		// Process base64 images with download capability and overlay buttons
		const base64ImagePattern = /!\[([^\]]*)\]\((data:image\/[^;]+;base64,[^)]+)\)/g;

		let processedContent = text.replace(base64ImagePattern, (match, alt, dataUrl) => {
			return `<div class="generated-media-container relative my-4 inline-block">
				<img src="${dataUrl}" alt="${alt || 'Generated image'}" class="max-w-full rounded-lg shadow-md border border-border" />
				<div class="media-overlay-buttons absolute top-2 right-2 flex gap-1 opacity-0 transition-opacity">
					<button onclick="(function(){
						const link = document.createElement('a');
						link.href = '${dataUrl}';
						link.download = '${alt || 'generated-image'}.png';
						link.click();
					})()" class="p-1.5 rounded bg-black/60 text-white hover:bg-black/80 transition-colors" title="Download image">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
						</svg>
					</button>
				</div>
			</div>`;
		});

		const generatedImagePattern = /!\[([^\]]*)\]\((\/api\/generated-images\/[^)]+)\)/g;
		processedContent = processedContent.replace(generatedImagePattern, (match, alt, imageUrl) => {
			const filename = imageUrl.split('/').pop() || 'generated-image.png';
			return `<div class="generated-media-container relative my-4 inline-block">
				<img src="${imageUrl}" alt="${alt || 'Generated image'}" class="max-w-full max-h-[500px] rounded-lg shadow-lg border border-border" />
				<div class="media-overlay-buttons absolute top-2 right-2 flex gap-1 opacity-0 transition-opacity">
					<a href="${imageUrl}" download="${filename}" class="p-1.5 rounded bg-black/60 text-white hover:bg-black/80 transition-colors no-underline" title="Download image">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
						</svg>
					</a>
				</div>
			</div>`;
		});

		const generatedVideoPattern = /\[([^\]]*)\]\((\/api\/generated-videos\/[^)]+)\)/g;
		processedContent = processedContent.replace(generatedVideoPattern, (match, _text, videoUrl) => {
			const filename = videoUrl.split('/').pop() || 'generated-video.mp4';
			return `<div class="generated-media-container relative my-4 inline-block">
				<video src="${videoUrl}" controls class="max-w-full max-h-[500px] rounded-lg shadow-lg border border-border">
					Your browser does not support the video tag.
				</video>
				<div class="media-overlay-buttons absolute top-2 right-2 flex gap-1 opacity-0 transition-opacity">
					<a href="${videoUrl}" download="${filename}" class="p-1.5 rounded bg-black/60 text-white hover:bg-black/80 transition-colors no-underline" title="Download video">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
						</svg>
					</a>
				</div>
			</div>`;
		});

		return marked(processedContent, { breaks: true }) as string;
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
			const audioUrl = URL.createObjectURL(audioBlob);

			const audio = new Audio(audioUrl);
			currentAudio = audio;

			audio.onended = () => {
				isReading = false;
				currentAudio = null;
				URL.revokeObjectURL(audioUrl);
			};

			audio.onerror = () => {
				console.error('Audio playback error');
				isReading = false;
				currentAudio = null;
				URL.revokeObjectURL(audioUrl);
			};

			await audio.play();
		} catch (e) {
			console.error('Failed to generate TTS:', e);
			isReading = false;
			currentAudio = null;
		}
	}

	// Cleanup on component destroy
	onDestroy(() => {
		if (currentAudio) {
			currentAudio.pause();
			currentAudio = null;
		}
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
		<div class="flex items-center gap-2 mb-1">
			<span class="font-semibold text-sm" style="color: var(--primary)">{profileName}</span>
			<span class="text-xs text-muted-foreground">{formatTime(timestamp)}</span>
			{#if streaming}
				<StreamingIndicator />
			{/if}
			{#if showActions && content && !streaming && hasFinalMetadata}
				<!-- Hover/touch action buttons - only shown on final messages with cost/duration -->
				<div class="ml-auto">
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
		<div class="overflow-hidden max-w-full">
			<div class="prose prose-sm max-w-none break-words pl-2" style="overflow-wrap: anywhere; word-break: break-word;">
				{#if content}
					{@html renderedHtml}
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
			<div class="mt-2 text-xs text-muted-foreground flex items-center gap-3">
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
			</div>
		{/if}
	</div>
</div>

<style>
	.message-container {
		position: relative;
	}
</style>
