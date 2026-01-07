<script lang="ts">
	/**
	 * ToolResultMessage - Standalone tool result display (fallback for ungrouped results)
	 */

	interface Props {
		content: string;
		toolId?: string;
	}

	let { content, toolId }: Props = $props();

	// Check if tool result contains media (image, video, or file)
	function toolResultHasMedia(text: string): boolean {
		try {
			const data = JSON.parse(text);
			return data.success && (
				data.image_url ||
				data.image_base64 ||
				data.video_url ||
				data.file_url
			);
		} catch {
			return false;
		}
	}

	function toolResultHasVideo(text: string): boolean {
		try {
			const data = JSON.parse(text);
			return data.success && data.video_url;
		} catch {
			return false;
		}
	}
</script>

<div class="w-full tool-result-message">
	<div class="min-w-0">
		<details class="w-full border border-border rounded-lg overflow-hidden shadow-s group">
			<summary class="w-full px-4 py-2 bg-muted/30 hover:bg-muted/50 flex items-center gap-2 cursor-pointer list-none transition-colors">
				<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
				</svg>
				<svg class="w-4 h-4 text-blue-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
				</svg>
				<span class="text-sm font-medium text-foreground">Tool Result</span>
				<span class="text-muted-foreground">&#8226;</span>
				{#if toolId}
					<span class="text-xs font-mono text-muted-foreground truncate max-w-[200px]">{toolId}</span>
					<span class="text-muted-foreground">&#8226;</span>
				{/if}
				<span class="text-green-500 text-sm">Success</span>
				<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</summary>
			<div class="px-4 py-3 bg-card border-t border-border">
				{#if toolResultHasMedia(content)}
					<!-- Show compact message for media - full media displayed outside tool group -->
					<span class="text-xs text-green-500">&#10003; {toolResultHasVideo(content) ? 'Video' : 'Image'} generated successfully (see below)</span>
				{:else}
					<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{content}</pre>
				{/if}
			</div>
		</details>
	</div>
</div>
