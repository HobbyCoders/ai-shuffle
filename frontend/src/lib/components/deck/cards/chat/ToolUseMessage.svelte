<script lang="ts">
	/**
	 * ToolUseMessage - Collapsible display for tool calls and results
	 */
	import type { ChatMessage } from '$lib/stores/tabs';

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	// Get status display info
	const statusInfo = $derived.by(() => {
		switch (message.toolStatus) {
			case 'running':
				return { text: 'Running', color: 'text-primary', isRunning: true, isError: false };
			case 'complete':
				return { text: 'Complete', color: 'text-green-500', isRunning: false, isError: false };
			case 'error':
				return { text: 'Error', color: 'text-red-500', isRunning: false, isError: true };
			default:
				return { text: 'Pending', color: 'text-muted-foreground', isRunning: false, isError: false };
		}
	});

	// Get icon based on tool name
	function getToolIcon(toolName: string): string {
		const name = toolName.toLowerCase();
		if (name.includes('bash') || name.includes('terminal')) {
			return 'M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z';
		}
		if (name.includes('read') || name.includes('file')) {
			return 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z';
		}
		if (name.includes('edit') || name.includes('write')) {
			return 'M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z';
		}
		if (name.includes('glob') || name.includes('search') || name.includes('grep')) {
			return 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z';
		}
		if (name.includes('web') || name.includes('fetch')) {
			return 'M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9';
		}
		if (name.includes('todo')) {
			return 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4';
		}
		// Default tool icon
		return 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z';
	}

	// Format tool input for display
	function formatToolInput(input: Record<string, unknown> | undefined): string {
		if (!input) return '';
		try {
			// Show compact representation for common fields
			if (input.file_path) return String(input.file_path);
			if (input.pattern) return String(input.pattern);
			if (input.command) {
				const cmd = String(input.command);
				return cmd.length > 100 ? cmd.substring(0, 100) + '...' : cmd;
			}
			if (input.query) {
				const q = String(input.query);
				return q.length > 100 ? q.substring(0, 100) + '...' : q;
			}
			// For other inputs, show JSON
			return JSON.stringify(input, null, 2);
		} catch {
			return String(input);
		}
	}

	// Truncate long content
	function truncateContent(content: string, maxLength: number = 1000): string {
		if (!content) return '';
		if (content.length <= maxLength) return content;
		return content.substring(0, maxLength) + '\n... [truncated]';
	}

	// Check if result contains an image
	function hasImageResult(content: string): boolean {
		try {
			const data = JSON.parse(content);
			return data.success && data.image_base64 && data.mime_type;
		} catch {
			return false;
		}
	}

	// Render image from base64 result
	function getImageDataUrl(content: string): string {
		try {
			const data = JSON.parse(content);
			if (data.success && data.image_base64 && data.mime_type) {
				return `data:${data.mime_type};base64,${data.image_base64}`;
			}
		} catch {
			// Not valid JSON
		}
		return '';
	}
</script>

<div class="w-full">
	<details class="border border-border rounded-lg overflow-hidden shadow-sm group">
		<summary class="w-full px-4 py-2 bg-muted/30 hover:bg-muted/50 flex items-center gap-2 cursor-pointer list-none transition-colors">
			<!-- Status icon -->
			{#if statusInfo.isRunning}
				<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
				</svg>
			{:else if statusInfo.isError}
				<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			{:else}
				<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
				</svg>
			{/if}

			<!-- Tool icon -->
			<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getToolIcon(message.toolName || '')} />
			</svg>

			<!-- Tool name -->
			<span class="text-sm font-medium text-foreground">{message.toolName || 'Tool'}</span>

			<!-- Status badge -->
			<span class="text-xs {statusInfo.color}">{statusInfo.text}</span>

			<!-- Chevron -->
			<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
			</svg>
		</summary>

		<!-- Expanded content -->
		<div class="bg-card border-t border-border">
			<div class="max-h-[24rem] overflow-y-auto">
				<!-- Input section -->
				{#if message.toolInput && Object.keys(message.toolInput).length > 0}
					<div class="px-4 py-3 border-b border-border/50">
						<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Input</div>
						<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{formatToolInput(message.toolInput)}</pre>
					</div>
				{/if}

				<!-- Result section -->
				{#if message.toolResult}
					<div class="px-4 py-3">
						<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Result</div>
						{#if hasImageResult(message.toolResult)}
							<div class="space-y-2">
								<div class="text-xs text-green-500">Image generated successfully</div>
								<img src={getImageDataUrl(message.toolResult)} alt="Generated" class="max-w-full max-h-64 rounded-lg border border-border" />
							</div>
						{:else}
							<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{truncateContent(message.toolResult)}</pre>
						{/if}
					</div>
				{:else if statusInfo.isRunning}
					<div class="px-4 py-3 text-sm text-muted-foreground flex items-center gap-2">
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
						Executing...
					</div>
				{/if}
			</div>
		</div>
	</details>
</div>
