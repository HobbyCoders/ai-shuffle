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

	// Tool category icons
	const TOOL_ICONS: Record<string, string> = {
		// Image generation
		generateImage: '🖼️', editImage: '🖼️', generateWithReference: '🖼️',
		// Video generation
		generateVideo: '🎬', imageToVideo: '🎬', bridgeFrames: '🎬', extendVideo: '🎬', analyzeVideo: '🎬',
		// 3D generation
		textTo3D: '🧊', imageTo3D: '🧊', retexture3D: '🧊', rig3D: '🧊', animate3D: '🧊', getTask3D: '🧊',
		// File operations
		Read: '📄', Write: '✏️', Edit: '✏️', NotebookEdit: '✏️',
		// Search
		Glob: '🔍', Grep: '🔍',
		// Terminal
		Bash: '💻',
		// Web
		WebFetch: '🌐', WebSearch: '🌐',
		// Agent
		Task: '🤖',
		// Todo
		TodoWrite: '📋',
	};

	function getToolIcon(toolName: string): string {
		return TOOL_ICONS[toolName] || '⚙️';
	}

	function getToolSummary(toolName: string, toolInput?: Record<string, unknown>): string {
		if (!toolInput) return '';

		const MAX_LEN = 50;
		const truncate = (s: string) => s.length > MAX_LEN ? s.slice(0, MAX_LEN) + '…' : s;
		const quote = (s: string) => `"${s}"`;

		switch (toolName) {
			// AI Image generation - show prompt
			case 'generateImage':
			case 'editImage':
			case 'generateWithReference':
			case 'textTo3D':
			case 'imageTo3D':
			case 'retexture3D':
				return truncate(quote(String(toolInput.prompt || toolInput.style_prompt || '')));

			// Video generation - show duration + prompt
			case 'generateVideo':
			case 'imageToVideo':
			case 'bridgeFrames':
			case 'extendVideo': {
				const dur = toolInput.duration ? `${toolInput.duration}s: ` : '';
				return truncate(`${dur}${quote(String(toolInput.prompt || ''))}`);
			}

			// Video analysis
			case 'analyzeVideo':
				return truncate(String(toolInput.video_path || ''));

			// 3D rigging/animation
			case 'rig3D':
			case 'animate3D':
			case 'getTask3D':
				return truncate(String(toolInput.task_id || toolInput.rig_task_id || toolInput.action_id || ''));

			// File operations - show path
			case 'Read':
			case 'Write':
			case 'Edit':
			case 'NotebookEdit':
				return truncate(String(toolInput.file_path || toolInput.notebook_path || ''));

			// Glob - show pattern
			case 'Glob':
				return truncate(String(toolInput.pattern || ''));

			// Grep - show pattern and optional path
			case 'Grep': {
				const pattern = quote(String(toolInput.pattern || ''));
				const path = toolInput.path ? ` in ${toolInput.path}` : '';
				return truncate(`${pattern}${path}`);
			}

			// Bash - show command
			case 'Bash':
				return truncate(String(toolInput.command || ''));

			// Web fetch - show hostname + path
			case 'WebFetch':
				try {
					const url = new URL(String(toolInput.url || ''));
					return truncate(url.hostname + url.pathname);
				} catch {
					return truncate(String(toolInput.url || ''));
				}

			// Web search - show query
			case 'WebSearch':
				return truncate(quote(String(toolInput.query || '')));

			// Task agent - show description
			case 'Task':
				return truncate(String(toolInput.description || toolInput.prompt || ''));

			// Todo - show count
			case 'TodoWrite': {
				const todos = toolInput.todos as Array<{ status?: string }> | undefined;
				if (Array.isArray(todos)) {
					const inProgress = todos.filter(t => t.status === 'in_progress').length;
					const completed = todos.filter(t => t.status === 'completed').length;
					return `${todos.length} tasks (${completed} done, ${inProgress} active)`;
				}
				return '';
			}

			default:
				// Try common field names as fallback
				for (const key of ['prompt', 'query', 'command', 'file_path', 'path', 'pattern', 'description']) {
					if (toolInput[key] && typeof toolInput[key] === 'string') {
						return truncate(String(toolInput[key]));
					}
				}
				return '';
		}
	}

	// Compute summary - use input if available, try parsing partialInput during streaming
	const summary = $derived.by(() => {
		if (input && Object.keys(input).length > 0) {
			return getToolSummary(name, input);
		}
		if (streaming && partialInput) {
			try {
				const parsed = JSON.parse(partialInput);
				return getToolSummary(name, parsed);
			} catch {
				// Partial JSON not parseable yet
				return '';
			}
		}
		return '';
	});

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
				<!-- Status indicator -->
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
				<!-- Tool icon -->
				<span class="text-sm flex-shrink-0" aria-hidden="true">{getToolIcon(name)}</span>
				<!-- Tool name -->
				<span class="text-sm font-medium text-foreground flex-shrink-0">{name}</span>
				<!-- Summary (if available) -->
				{#if summary}
					<span class="text-muted-foreground flex-shrink-0">•</span>
					<span
						class="text-sm text-muted-foreground truncate min-w-0"
						title={getToolSummary(name, input)}
					>{summary}</span>
				{/if}
				<!-- Status badge for running/error states -->
				{#if status === 'running' || streaming}
					<span class="text-primary text-xs flex-shrink-0 ml-auto">(running)</span>
				{:else if status === 'error'}
					<span class="text-red-500 text-xs flex-shrink-0 ml-auto">(error)</span>
				{/if}
				<!-- Chevron -->
				<svg class="w-4 h-4 text-muted-foreground transition-transform group-open:rotate-180 flex-shrink-0 {status !== 'running' && status !== 'error' && !streaming ? 'ml-auto' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
