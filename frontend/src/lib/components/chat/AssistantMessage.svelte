<script lang="ts">
	/**
	 * AssistantMessage - Assistant message with markdown rendering, copy, and fork buttons
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
		messageIndex: number;
		messageId: string;
		sessionId?: string | null;
		streaming?: boolean;
		hasPartialMessages?: boolean;
		metadata?: Record<string, unknown>;
		onCopy?: (content: string, messageId: string) => void;
		onFork?: (sessionId: string, messageIndex: number, messageId: string) => void;
	}

	let {
		content,
		timestamp,
		messageIndex,
		messageId,
		sessionId,
		streaming = false,
		hasPartialMessages = true,
		metadata,
		onCopy,
		onFork
	}: Props = $props();

	let copiedMessageId = $state<string | null>(null);
	let forkingMessageId = $state<string | null>(null);

	function formatTime(date?: Date): string {
		const d = date || new Date();
		return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
	}

	function formatCost(cost: number | undefined): string {
		if (cost === undefined) return '';
		return `$${cost.toFixed(4)}`;
	}

	function renderMarkdown(text: string, isStreaming: boolean = false): string {
		// If streaming, hide media content to prevent flickering - show placeholder instead
		if (isStreaming) {
			// Replace image markdown with placeholder
			let processedContent = text.replace(/!\[([^\]]*)\]\((data:image\/[^;]+;base64,[^)]+)\)/g,
				'<div class="media-placeholder my-4 p-4 bg-muted/30 rounded-lg border border-border text-center text-muted-foreground text-sm">Image loading...</div>');
			processedContent = processedContent.replace(/!\[([^\]]*)\]\((\/api\/generated-images\/[^)]+)\)/g,
				'<div class="media-placeholder my-4 p-4 bg-muted/30 rounded-lg border border-border text-center text-muted-foreground text-sm">Image loading...</div>');
			processedContent = processedContent.replace(/\[([^\]]*)\]\((\/api\/generated-videos\/[^)]+)\)/g,
				'<div class="media-placeholder my-4 p-4 bg-muted/30 rounded-lg border border-border text-center text-muted-foreground text-sm">Video loading...</div>');
			processedContent = processedContent.replace(/"image_base64"\s*:\s*"([A-Za-z0-9+/=]{100,})"/g,
				'<div class="media-placeholder my-4 p-4 bg-muted/30 rounded-lg border border-border text-center text-muted-foreground text-sm">Image loading...</div>');
			// Replace file download links with placeholder
			processedContent = processedContent.replace(/[^\]]+\]\((\/api\/files\/[^)]+)\)/g,
				'<div class="media-placeholder my-4 p-4 bg-muted/30 rounded-lg border border-border text-center text-muted-foreground text-sm">File preparing...</div>');
			return marked(processedContent, { breaks: true }) as string;
		}

		// Process base64 images with download capability and overlay buttons
		const base64ImagePattern = /!\[([^\]]*)\]\((data:image\/[^;]+;base64,[^)]+)\)/g;

		// Replace base64 image markdown with custom HTML that includes overlay buttons
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

		// Render generated-images markdown with overlay buttons
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

		// Render generated-videos markdown links as video player with overlay buttons
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

	async function handleCopy() {
		if (onCopy) {
			onCopy(content, messageId);
		} else {
			try {
				await navigator.clipboard.writeText(content);
				copiedMessageId = messageId;
				setTimeout(() => {
					copiedMessageId = null;
				}, 2000);
			} catch (e) {
				console.error('Failed to copy to clipboard:', e);
			}
		}
	}

	async function handleFork() {
		if (!sessionId || forkingMessageId) return;

		if (onFork) {
			forkingMessageId = messageId;
			onFork(sessionId, messageIndex, messageId);
			// The parent will handle resetting this via state
			setTimeout(() => {
				forkingMessageId = null;
			}, 5000);
		}
	}
</script>

<div class="w-full assistant-message">
	<div class="min-w-0">
		<div class="flex items-center gap-2 mb-1">
			<span class="font-semibold text-sm text-orange-400">Claude</span>
			<span class="text-xs text-muted-foreground">{formatTime(timestamp)}</span>
			{#if streaming}
				<StreamingIndicator />
			{/if}
			{#if content && !streaming}
				<!-- Action buttons container -->
				<div class="ml-auto flex items-center gap-1">
					<!-- Copy response button -->
					<button
						class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors"
						onclick={handleCopy}
						title="Copy response"
					>
						{#if copiedMessageId === messageId}
							<svg class="w-3.5 h-3.5 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
							<span class="text-green-500">Copied</span>
						{:else}
							<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
							</svg>
							<span>Copy</span>
						{/if}
					</button>
					<!-- Fork from here button -->
					{#if sessionId}
						<button
							class="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground transition-colors"
							onclick={handleFork}
							title="Fork conversation from this point"
							disabled={forkingMessageId === messageId}
						>
							{#if forkingMessageId === messageId}
								<svg class="w-3.5 h-3.5 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
								</svg>
								<span>Forking...</span>
							{:else}
								<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7v8a2 2 0 002 2h6M8 7V5a2 2 0 012-2h4.586a1 1 0 01.707.293l4.414 4.414a1 1 0 01.293.707V15a2 2 0 01-2 2h-2M8 7H6a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2v-2" />
								</svg>
								<span>Fork</span>
							{/if}
						</button>
					{/if}
				</div>
			{/if}
		</div>
		<div class="overflow-hidden">
			<div class="prose prose-sm max-w-none break-words overflow-x-auto">
				{#if content}
					{@html renderMarkdown(content, streaming)}
					{#if streaming}
						<span class="inline-block w-2 h-4 ml-0.5 bg-primary animate-pulse"></span>
					{/if}
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
				{/if}
				{#if metadata.duration_ms}
					<span>{((metadata.duration_ms as number) / 1000).toFixed(1)}s</span>
				{/if}
			</div>
		{/if}
	</div>
</div>
