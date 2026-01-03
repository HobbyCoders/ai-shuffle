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

	// Check if this is an Edit tool with old_string/new_string
	const isEditTool = $derived(name === 'Edit' && input?.old_string !== undefined && input?.new_string !== undefined);
	const editFilePath = $derived(isEditTool ? String(input?.file_path || '') : '');
	const oldString = $derived(isEditTool ? String(input?.old_string || '') : '');
	const newString = $derived(isEditTool ? String(input?.new_string || '') : '');

	// Simple line-based diff for Edit tool
	function computeDiff(oldStr: string, newStr: string): Array<{ type: 'context' | 'removed' | 'added'; line: string }> {
		const oldLines = oldStr.split('\n');
		const newLines = newStr.split('\n');
		const result: Array<{ type: 'context' | 'removed' | 'added'; line: string }> = [];

		// Simple diff: show all old lines as removed, all new lines as added
		// For a more sophisticated diff, we'd use a proper diff algorithm
		const maxLen = Math.max(oldLines.length, newLines.length);
		let i = 0, j = 0;

		// Find common prefix
		while (i < oldLines.length && i < newLines.length && oldLines[i] === newLines[i]) {
			result.push({ type: 'context', line: oldLines[i] });
			i++;
			j++;
		}

		// Find common suffix
		let oldEnd = oldLines.length - 1;
		let newEnd = newLines.length - 1;
		const suffixLines: Array<{ type: 'context' | 'removed' | 'added'; line: string }> = [];
		while (oldEnd > i && newEnd > j && oldLines[oldEnd] === newLines[newEnd]) {
			suffixLines.unshift({ type: 'context', line: oldLines[oldEnd] });
			oldEnd--;
			newEnd--;
		}

		// Everything in between is changed
		for (let k = i; k <= oldEnd; k++) {
			result.push({ type: 'removed', line: oldLines[k] });
		}
		for (let k = j; k <= newEnd; k++) {
			result.push({ type: 'added', line: newLines[k] });
		}

		result.push(...suffixLines);
		return result;
	}

	const diffLines = $derived(isEditTool ? computeDiff(oldString, newString) : []);

	// Tool icon categories - returns icon type for SVG rendering
	type IconType = 'image' | 'video' | 'cube' | 'file' | 'pencil' | 'search' | 'terminal' | 'globe' | 'bot' | 'list' | 'cog';

	const TOOL_ICON_MAP: Record<string, IconType> = {
		// Image generation
		generateImage: 'image', editImage: 'image', generateWithReference: 'image',
		// Video generation
		generateVideo: 'video', imageToVideo: 'video', bridgeFrames: 'video', extendVideo: 'video', analyzeVideo: 'video',
		// 3D generation
		textTo3D: 'cube', imageTo3D: 'cube', retexture3D: 'cube', rig3D: 'cube', animate3D: 'cube', getTask3D: 'cube',
		// File operations
		Read: 'file', Write: 'pencil', Edit: 'pencil', NotebookEdit: 'pencil',
		// Search
		Glob: 'search', Grep: 'search',
		// Terminal
		Bash: 'terminal',
		// Web
		WebFetch: 'globe', WebSearch: 'globe',
		// Agent
		Task: 'bot',
		// Todo
		TodoWrite: 'list',
	};

	function getToolIconType(toolName: string): IconType {
		return TOOL_ICON_MAP[toolName] || 'cog';
	}

	function getToolSummary(toolInput?: Record<string, unknown>): string {
		if (!toolInput) return '';
		// Use the description field that tools provide, fallback to path fields
		return String(toolInput.description || toolInput.file_path || toolInput.path || toolInput.notebook_path || '');
	}

	// Extract field from partial JSON string using regex (for streaming)
	function extractFieldFromPartial(partial: string, fieldName: string): string {
		// Match "fieldName": "value" or "fieldName": 'value' patterns
		// Handle escaped quotes in the value
		const regex = new RegExp(`"${fieldName}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`);
		const match = partial.match(regex);
		if (match) {
			// Unescape the value
			return match[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
		}
		return '';
	}

	// Extract summary from partial JSON during streaming
	function getPartialSummary(partial: string): string {
		// Try to parse as complete JSON first
		try {
			const parsed = JSON.parse(partial);
			return getToolSummary(parsed);
		} catch {
			// Fallback: extract fields via regex from incomplete JSON
			const description = extractFieldFromPartial(partial, 'description');
			if (description) return description;

			const filePath = extractFieldFromPartial(partial, 'file_path');
			if (filePath) return filePath;

			const path = extractFieldFromPartial(partial, 'path');
			if (path) return path;

			const notebookPath = extractFieldFromPartial(partial, 'notebook_path');
			if (notebookPath) return notebookPath;

			return '';
		}
	}

	// Compute summary - use input if available, try parsing partialInput during streaming
	const summary = $derived.by(() => {
		if (input && Object.keys(input).length > 0) {
			return getToolSummary(input);
		}
		if (streaming && partialInput) {
			return getPartialSummary(partialInput);
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
				{#if getToolIconType(name) === 'image'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
						<circle cx="8.5" cy="8.5" r="1.5"/>
						<polyline points="21 15 16 10 5 21"/>
					</svg>
				{:else if getToolIconType(name) === 'video'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<polygon points="23 7 16 12 23 17 23 7"/>
						<rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
					</svg>
				{:else if getToolIconType(name) === 'cube'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
						<polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
						<line x1="12" y1="22.08" x2="12" y2="12"/>
					</svg>
				{:else if getToolIconType(name) === 'file'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
						<polyline points="14 2 14 8 20 8"/>
						<line x1="16" y1="13" x2="8" y2="13"/>
						<line x1="16" y1="17" x2="8" y2="17"/>
					</svg>
				{:else if getToolIconType(name) === 'pencil'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>
					</svg>
				{:else if getToolIconType(name) === 'search'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="11" cy="11" r="8"/>
						<line x1="21" y1="21" x2="16.65" y2="16.65"/>
					</svg>
				{:else if getToolIconType(name) === 'terminal'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<polyline points="4 17 10 11 4 5"/>
						<line x1="12" y1="19" x2="20" y2="19"/>
					</svg>
				{:else if getToolIconType(name) === 'globe'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="12" cy="12" r="10"/>
						<line x1="2" y1="12" x2="22" y2="12"/>
						<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
					</svg>
				{:else if getToolIconType(name) === 'bot'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<rect x="3" y="11" width="18" height="10" rx="2"/>
						<circle cx="12" cy="5" r="2"/>
						<path d="M12 7v4"/>
						<line x1="8" y1="16" x2="8" y2="16"/>
						<line x1="16" y1="16" x2="16" y2="16"/>
					</svg>
				{:else if getToolIconType(name) === 'list'}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<line x1="8" y1="6" x2="21" y2="6"/>
						<line x1="8" y1="12" x2="21" y2="12"/>
						<line x1="8" y1="18" x2="21" y2="18"/>
						<line x1="3" y1="6" x2="3.01" y2="6"/>
						<line x1="3" y1="12" x2="3.01" y2="12"/>
						<line x1="3" y1="18" x2="3.01" y2="18"/>
					</svg>
				{:else}
					<svg class="w-4 h-4 text-yellow-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="12" cy="12" r="3"/>
						<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
					</svg>
				{/if}
				<!-- Tool name -->
				<span class="text-sm font-medium text-foreground flex-shrink-0">{name}</span>
				<!-- Summary (if available) -->
				{#if summary}
					<span class="text-muted-foreground flex-shrink-0">•</span>
					<span
						class="text-sm text-muted-foreground truncate min-w-0"
						title={summary}
					>{summary}</span>
				{/if}
				<!-- Chevron -->
				<svg class="w-4 h-4 text-muted-foreground transition-transform group-open:rotate-180 flex-shrink-0 ml-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</summary>
			<div class="bg-card border-t border-border">
				{#if isEditTool}
					<!-- Side-by-side diff view for Edit tool -->
					<div class="px-3 py-2">
						<div class="grid grid-cols-2 gap-2">
							<!-- Old (removed) -->
							<div class="min-w-0">
								<div class="text-[10px] text-red-400 mb-1 font-medium flex items-center gap-1">
									<span>−</span>
									<span>Removed</span>
								</div>
								<div class="rounded border border-red-500/20 bg-red-500/5 overflow-hidden max-h-48 overflow-auto">
									<pre class="text-[10px] font-mono px-1.5 py-1 text-red-400 m-0">{oldString || ' '}</pre>
								</div>
							</div>
							<!-- New (added) -->
							<div class="min-w-0">
								<div class="text-[10px] text-green-400 mb-1 font-medium flex items-center gap-1">
									<span>+</span>
									<span>Added</span>
								</div>
								<div class="rounded border border-green-500/20 bg-green-500/5 overflow-hidden max-h-48 overflow-auto">
									<pre class="text-[10px] font-mono px-1.5 py-1 text-green-400 m-0">{newString || ' '}</pre>
								</div>
							</div>
						</div>
					</div>
				{:else if partialInput || input}
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
				{#if result && !isEditTool}
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
