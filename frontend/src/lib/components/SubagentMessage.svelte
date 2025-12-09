<script lang="ts">
	/**
	 * SubagentMessage - Display for subagent work (matching tool message group style)
	 */
	import type { ChatMessage } from '$lib/stores/tabs';

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	// Count children by type
	const childCounts = $derived(() => {
		const children = message.agentChildren || [];
		const toolUseChildren = children.filter(c => c.type === 'tool_use');
		const completedTools = toolUseChildren.filter(c => c.toolStatus === 'complete').length;

		return {
			total: children.length,
			toolUse: toolUseChildren.length,
			completedTools
		};
	});

	// Get status display
	const statusInfo = $derived(() => {
		switch (message.agentStatus) {
			case 'running':
				return { text: 'Executing...', color: 'text-primary', isRunning: true, isError: false };
			case 'completed':
				return { text: 'Complete', color: 'text-green-500', isRunning: false, isError: false };
			case 'error':
				return { text: 'Error', color: 'text-red-500', isRunning: false, isError: true };
			default:
				return { text: 'Pending', color: 'text-muted-foreground', isRunning: false, isError: false };
		}
	});

	// Get model display name
	function getModelBadge(agentType?: string): { label: string; color: string } {
		switch (agentType?.toLowerCase()) {
			case 'explore':
			case 'plan':
				return { label: 'Fast', color: 'bg-blue-500/20 text-blue-500' };
			default:
				return { label: agentType || 'Agent', color: 'bg-purple-500/20 text-purple-500' };
		}
	}

	function formatToolInput(input: Record<string, unknown> | undefined): string {
		if (!input) return '';
		try {
			if (input.file_path) return String(input.file_path);
			if (input.pattern) return String(input.pattern);
			if (input.command) return String(input.command).substring(0, 100);
			if (input.query) return String(input.query).substring(0, 100);
			return JSON.stringify(input, null, 2);
		} catch {
			return String(input);
		}
	}

	function truncateContent(content: string, maxLength: number = 500): string {
		if (content.length <= maxLength) return content;
		return content.substring(0, maxLength) + '...';
	}

	// Check if tool result contains an image
	function toolResultHasImage(content: string): boolean {
		try {
			const data = JSON.parse(content);
			return data.success && data.image_base64 && data.mime_type;
		} catch {
			return false;
		}
	}

	// Render tool result content, detecting and displaying images
	function renderToolResult(content: string): string {
		try {
			const data = JSON.parse(content);
			if (data.success && data.image_base64 && data.mime_type) {
				const mimeType = data.mime_type || 'image/png';
				const dataUrl = `data:${mimeType};base64,${data.image_base64}`;
				return `<div class="generated-image-result">
					<div class="mb-2 text-xs text-green-500">✓ Image generated successfully</div>
					<img src="${dataUrl}" alt="Generated image" class="max-w-full max-h-64 rounded-lg shadow-md border border-border" />
					<div class="flex gap-2 mt-2">
						<button onclick="(function(){
							const link = document.createElement('a');
							link.href = '${dataUrl}';
							link.download = 'generated-image.png';
							link.click();
						})()" class="text-xs px-2 py-1 bg-primary text-primary-foreground rounded hover:opacity-90 flex items-center gap-1">
							<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
							Download
						</button>
					</div>
				</div>`;
			}
		} catch {
			// Not JSON
		}
		return '';
	}
</script>

<details class="w-full border border-border rounded-lg overflow-hidden shadow-s group">
	<summary class="w-full px-4 py-2 bg-muted/30 hover:bg-muted/50 flex items-center gap-2 cursor-pointer list-none transition-colors">
		<!-- Status icon -->
		{#if statusInfo().isRunning}
			<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
			</svg>
		{:else if statusInfo().isError}
			<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
			</svg>
		{:else}
			<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
			</svg>
		{/if}

		<!-- Subagent icon -->
		<svg class="w-4 h-4 text-indigo-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
		</svg>

		<!-- Agent type badge -->
		{#if message.agentType}
			{@const badge = getModelBadge(message.agentType)}
			<span class="px-1.5 py-0.5 text-xs font-medium {badge.color} rounded-full">
				{badge.label}
			</span>
		{/if}

		<!-- Description or default label -->
		<span class="text-sm font-medium text-foreground truncate">
			{message.agentDescription || 'Subagent'}
		</span>

		<span class="text-muted-foreground">•</span>

		<!-- Status text -->
		<span class="{statusInfo().color} text-sm">{statusInfo().text}</span>

		<!-- Tool count (if any) -->
		{#if childCounts().toolUse > 0}
			<span class="text-muted-foreground">•</span>
			<span class="text-xs text-muted-foreground">
				{childCounts().completedTools}/{childCounts().toolUse} tools
			</span>
		{/if}

		<!-- Chevron -->
		<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
		</svg>
	</summary>

	<!-- Expanded content -->
	<div class="bg-card border-t border-border">
		<div class="max-h-[32rem] overflow-y-auto">
			<!-- Initial prompt section -->
			{#if message.agentPrompt}
				<div class="px-4 py-3 border-b border-border/50">
					<div class="text-xs text-muted-foreground mb-1 font-medium">Prompt</div>
					<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{message.agentPrompt}</pre>
				</div>
			{/if}

			<!-- Agent children (tool calls and text messages) -->
			{#if message.agentChildren && message.agentChildren.length > 0}
				{#each message.agentChildren as child (child.id)}
					{#if child.type === 'tool_use'}
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<details class="group/tool">
								<summary class="flex items-center gap-2 cursor-pointer list-none hover:bg-muted/30 -mx-1 px-1 py-1 rounded">
									{#if child.toolStatus === 'running'}
										<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
										</svg>
									{:else if child.toolStatus === 'error'}
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
									<span class="text-sm font-medium text-foreground">{child.toolName || 'Tool'}</span>
									<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open/tool:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
									</svg>
								</summary>
								<div class="mt-2 space-y-2">
									{#if child.toolInput}
										<div class="p-2 bg-muted/50 rounded">
											<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Input</div>
											<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{formatToolInput(child.toolInput)}</pre>
										</div>
									{/if}
									{#if child.toolResult}
										<div class="p-2 bg-muted/50 rounded">
											<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Result</div>
											{#if toolResultHasImage(child.toolResult)}
												{@html renderToolResult(child.toolResult)}
											{:else}
												<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{truncateContent(child.toolResult)}</pre>
											{/if}
										</div>
									{/if}
								</div>
							</details>
						</div>
					{:else if child.type === 'tool_result'}
						<!-- Standalone tool result (fallback) -->
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<details class="group/result">
								<summary class="flex items-center gap-2 cursor-pointer list-none hover:bg-muted/30 -mx-1 px-1 py-1 rounded">
									<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
									</svg>
									<span class="text-sm font-medium text-muted-foreground">Result</span>
									{#if child.toolName}
										<span class="text-xs text-muted-foreground/70">({child.toolName})</span>
									{/if}
									<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open/result:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
									</svg>
								</summary>
								{#if child.content}
									{#if toolResultHasImage(child.content)}
										<div class="mt-2 p-2 bg-muted/50 rounded">
											{@html renderToolResult(child.content)}
										</div>
									{:else}
										<pre class="mt-2 p-2 bg-muted/50 rounded text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{truncateContent(child.content)}</pre>
									{/if}
								{/if}
							</details>
						</div>
					{:else if child.type === 'text'}
						<!-- Text child (subagent's text output) -->
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<div class="flex items-start gap-2">
								<svg class="w-4 h-4 text-purple-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" />
								</svg>
								<div class="text-sm text-foreground whitespace-pre-wrap break-words">
									{child.content}
								</div>
							</div>
						</div>
					{/if}
				{/each}
			{:else if message.streaming}
				<div class="px-4 py-3 text-sm text-muted-foreground flex items-center gap-2">
					<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
					</svg>
					Working...
				</div>
			{:else if !message.agentPrompt}
				<div class="px-4 py-3 text-sm text-muted-foreground">
					No activity recorded
				</div>
			{/if}

			<!-- Final result section -->
			{#if message.content && message.agentStatus !== 'running'}
				<div class="px-4 py-3 border-t border-border/50">
					<div class="text-xs text-muted-foreground mb-1 font-medium">Result</div>
					{#if toolResultHasImage(message.content)}
						{@html renderToolResult(message.content)}
					{:else}
						<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{message.content}</pre>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</details>
