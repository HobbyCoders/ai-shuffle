<script lang="ts">
	/**
	 * SubagentMessage - Enhanced display for subagent work
	 *
	 * Features:
	 * - Live progress tracking with tool call visualization
	 * - Model badge showing which model is being used
	 * - Collapsible with smooth transitions
	 * - Mobile-responsive design
	 */
	import type { ChatMessage, SubagentChildMessage } from '$lib/stores/tabs';

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	// Track expanded state - auto-expand when running
	let isExpanded = $state(message.agentStatus === 'running');

	// Compute status display
	const statusConfig = $derived(() => {
		switch (message.agentStatus) {
			case 'running':
				return { text: 'Running...', color: 'text-primary', bgColor: 'bg-primary/10', borderColor: 'border-primary/30', icon: 'spinner' };
			case 'completed':
				return { text: 'Completed', color: 'text-green-500', bgColor: 'bg-green-500/10', borderColor: 'border-green-500/30', icon: 'check' };
			case 'error':
				return { text: 'Error', color: 'text-red-500', bgColor: 'bg-red-500/10', borderColor: 'border-red-500/30', icon: 'error' };
			default:
				return { text: 'Pending', color: 'text-muted-foreground', bgColor: 'bg-muted/30', borderColor: 'border-border', icon: 'pending' };
		}
	});

	// Count children by type
	const childCounts = $derived(() => {
		const children = message.agentChildren || [];
		const toolUseChildren = children.filter(c => c.type === 'tool_use');
		const runningTools = toolUseChildren.filter(c => c.toolStatus === 'running').length;
		const completedTools = toolUseChildren.filter(c => c.toolStatus === 'complete').length;

		return {
			total: children.length,
			toolUse: toolUseChildren.length,
			toolResult: children.filter(c => c.type === 'tool_result').length,
			text: children.filter(c => c.type === 'text').length,
			runningTools,
			completedTools
		};
	});

	// Progress percentage
	const progressPercent = $derived(() => {
		if (childCounts().toolUse === 0) return 0;
		return Math.round((childCounts().completedTools / childCounts().toolUse) * 100);
	});

	// Get model display name
	function getModelBadge(agentType?: string): { label: string; color: string } {
		// Agent types often indicate model in the SDK
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
			// Smart formatting for common tool inputs
			if (input.file_path) return String(input.file_path);
			if (input.pattern) return String(input.pattern);
			if (input.command) return String(input.command).substring(0, 100);
			if (input.query) return String(input.query).substring(0, 100);
			return JSON.stringify(input, null, 2);
		} catch {
			return String(input);
		}
	}

	function getToolInputPreview(input: Record<string, unknown> | undefined): string {
		if (!input) return '';
		// Return a short preview for the collapsed view
		if (input.file_path) return String(input.file_path);
		if (input.pattern) return String(input.pattern);
		if (input.command) return String(input.command).substring(0, 50) + (String(input.command).length > 50 ? '...' : '');
		return '';
	}

	function truncateContent(content: string, maxLength: number = 500): string {
		if (content.length <= maxLength) return content;
		return content.substring(0, maxLength) + '...';
	}
</script>

<!-- Main subagent card -->
<div class="w-full border {statusConfig().borderColor} rounded-lg overflow-hidden shadow-sm {statusConfig().bgColor}">
	<!-- Header - always visible -->
	<button
		onclick={() => isExpanded = !isExpanded}
		class="w-full px-3 sm:px-4 py-2.5 sm:py-3 flex items-center gap-2 sm:gap-3 cursor-pointer hover:bg-muted/30 transition-colors text-left"
	>
		<!-- Status icon -->
		{#if statusConfig().icon === 'spinner'}
			<svg class="w-5 h-5 {statusConfig().color} animate-spin flex-shrink-0" fill="none" viewBox="0 0 24 24">
				<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
				<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
			</svg>
		{:else if statusConfig().icon === 'check'}
			<svg class="w-5 h-5 {statusConfig().color} flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
				<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
			</svg>
		{:else if statusConfig().icon === 'error'}
			<svg class="w-5 h-5 {statusConfig().color} flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
				<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
			</svg>
		{:else}
			<svg class="w-5 h-5 {statusConfig().color} flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
				<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
			</svg>
		{/if}

		<!-- Agent info -->
		<div class="flex-1 min-w-0">
			<div class="flex items-center gap-1.5 sm:gap-2 flex-wrap">
				<!-- Agent type badge -->
				{#if message.agentType}
					{@const badge = getModelBadge(message.agentType)}
					<span class="px-1.5 sm:px-2 py-0.5 text-xs font-medium {badge.color} rounded-full">
						{badge.label}
					</span>
				{/if}
				<span class="text-sm {statusConfig().color}">
					{statusConfig().text}
				</span>
			</div>
			{#if message.agentDescription}
				<p class="text-xs sm:text-sm text-muted-foreground mt-0.5 line-clamp-1 sm:line-clamp-none sm:truncate">
					{message.agentDescription}
				</p>
			{/if}
		</div>

		<!-- Progress indicator (when running) -->
		{#if message.agentStatus === 'running' && childCounts().toolUse > 0}
			<div class="hidden sm:flex items-center gap-2 text-xs text-muted-foreground">
				<div class="w-16 h-1.5 bg-muted rounded-full overflow-hidden">
					<div
						class="h-full bg-primary transition-all duration-300"
						style="width: {progressPercent()}%"
					></div>
				</div>
				<span>{childCounts().completedTools}/{childCounts().toolUse}</span>
			</div>
		{:else if childCounts().toolUse > 0}
			<!-- Stats badge (when not running) -->
			<div class="hidden sm:flex items-center gap-2 text-xs text-muted-foreground">
				<span>{childCounts().toolUse} tool{childCounts().toolUse !== 1 ? 's' : ''}</span>
			</div>
		{/if}

		<!-- Expand chevron -->
		<svg
			class="w-5 h-5 text-muted-foreground transition-transform flex-shrink-0 {isExpanded ? 'rotate-180' : ''}"
			fill="none"
			stroke="currentColor"
			viewBox="0 0 24 24"
		>
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
		</svg>
	</button>

	<!-- Mobile progress bar (shown below header when running) -->
	{#if message.agentStatus === 'running' && childCounts().toolUse > 0}
		<div class="sm:hidden px-3 pb-2">
			<div class="flex items-center gap-2 text-xs text-muted-foreground">
				<div class="flex-1 h-1 bg-muted rounded-full overflow-hidden">
					<div
						class="h-full bg-primary transition-all duration-300"
						style="width: {progressPercent()}%"
					></div>
				</div>
				<span>{childCounts().completedTools}/{childCounts().toolUse}</span>
			</div>
		</div>
	{/if}

	<!-- Expanded content -->
	{#if isExpanded}
		<div class="border-t border-border bg-card">
			<!-- Agent children -->
			{#if message.agentChildren && message.agentChildren.length > 0}
				<div class="max-h-96 overflow-y-auto">
					{#each message.agentChildren as child (child.id)}
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							{#if child.type === 'tool_use'}
								<!-- Tool use with grouped result -->
								<details class="group">
									<summary class="flex items-center gap-2 cursor-pointer list-none hover:bg-muted/30 -mx-1 px-1 py-1 rounded">
										{#if child.toolStatus === 'running'}
											<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" viewBox="0 0 24 24">
												<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
												<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
											</svg>
										{:else if child.toolStatus === 'error'}
											<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
												<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd" />
											</svg>
										{:else}
											<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
												<path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd" />
											</svg>
										{/if}
										<svg class="w-4 h-4 text-blue-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										</svg>
										<span class="text-sm font-medium text-foreground">{child.toolName || 'Tool'}</span>
										<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
										</svg>
									</summary>
									<div class="mt-2 space-y-2">
										{#if child.toolInput}
											<div class="p-2 bg-muted/50 rounded">
												<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Input</div>
												<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32">{formatToolInput(child.toolInput)}</pre>
											</div>
										{/if}
										{#if child.toolResult}
											<div class="p-2 bg-muted/50 rounded">
												<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Result</div>
												<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap">{truncateContent(child.toolResult)}</pre>
											</div>
										{/if}
									</div>
								</details>
							{:else if child.type === 'tool_result'}
								<!-- Standalone tool result (fallback for ungrouped results) -->
								<details class="group">
									<summary class="flex items-center gap-2 cursor-pointer list-none hover:bg-muted/30 -mx-1 px-1 py-1 rounded">
										<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
											<path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd" />
										</svg>
										<span class="text-sm font-medium text-muted-foreground">Result</span>
										{#if child.toolName}
											<span class="text-xs text-muted-foreground/70">({child.toolName})</span>
										{/if}
										<svg class="w-4 h-4 text-muted-foreground ml-auto transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
										</svg>
									</summary>
									{#if child.content}
										<pre class="mt-2 p-2 bg-muted/50 rounded text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap">{truncateContent(child.content)}</pre>
									{/if}
								</details>
							{:else if child.type === 'text'}
								<!-- Text child -->
								<div class="text-sm text-foreground whitespace-pre-wrap">
									{child.content}
								</div>
							{/if}
						</div>
					{/each}
				</div>
			{:else if message.streaming}
				<div class="px-4 py-3 text-sm text-muted-foreground flex items-center gap-2">
					<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
					</svg>
					Working...
				</div>
			{:else}
				<div class="px-4 py-3 text-sm text-muted-foreground">
					No activity recorded
				</div>
			{/if}

		</div>
	{/if}
</div>
