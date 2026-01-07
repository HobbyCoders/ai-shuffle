<script lang="ts">
	/**
	 * ToolUseMessage - Collapsible tool execution display
	 */

	interface Props {
		name: string;
		input?: Record<string, unknown>;
		partialInput?: string;
		result?: string;
		status: 'running' | 'complete' | 'error';
		streaming?: boolean;
	}

	let { name, input, partialInput, result, status, streaming = false }: Props = $props();

	// Check if tool result contains media (image, video, or file)
	function toolResultHasMedia(content: string): boolean {
		try {
			const data = JSON.parse(content);
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

	function toolResultHasVideo(content: string): boolean {
		try {
			const data = JSON.parse(content);
			return data.success && data.video_url;
		} catch {
			return false;
		}
	}
</script>

<div class="w-full tool-use-message">
	<div class="min-w-0">
		<details class="w-full border border-border rounded-lg overflow-hidden shadow-s group">
			<summary class="w-full px-4 py-2 bg-muted/30 hover:bg-muted/50 flex items-center gap-2 cursor-pointer list-none transition-colors">
				{#if status === 'running' || streaming}
					<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
					</svg>
				{:else if status === 'error'}
					<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				{:else}
					<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{/if}
				<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
				</svg>
				<span class="text-sm font-medium text-foreground">{name}</span>
				<span class="text-muted-foreground">&#8226;</span>
				{#if status === 'running' || streaming}
					<span class="text-primary text-sm">Executing...</span>
				{:else if status === 'error'}
					<span class="text-red-500 text-sm">Error</span>
				{:else}
					<span class="text-green-500 text-sm">Complete</span>
				{/if}
				<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</summary>
			<div class="bg-card border-t border-border">
				{#if partialInput || input}
					<div class="px-4 py-3 border-b border-border/50">
						<div class="text-xs text-muted-foreground mb-1 font-medium">Input</div>
						{#if streaming && partialInput}
							<!-- Show raw streaming input with cursor -->
							<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{partialInput}<span class="inline-block w-1.5 h-3 ml-0.5 bg-primary animate-pulse"></span></pre>
						{:else if input && Object.keys(input).length > 0}
							<!-- Show formatted JSON when complete -->
							<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{JSON.stringify(input, null, 2)}</pre>
						{:else if partialInput}
							<!-- Fallback: show partial input if toolInput is empty but we have partial -->
							<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{partialInput}</pre>
						{/if}
					</div>
				{/if}
				{#if result}
					<div class="px-4 py-3">
						<div class="text-xs text-muted-foreground mb-1 font-medium">Result</div>
						{#if toolResultHasMedia(result)}
							<!-- Show compact message for media - full media displayed outside tool group -->
							<span class="text-xs text-green-500">&#10003; {toolResultHasVideo(result) ? 'Video' : 'Image'} generated successfully (see below)</span>
						{:else}
							<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{result}</pre>
						{/if}
					</div>
				{/if}
			</div>
		</details>
	</div>
</div>
