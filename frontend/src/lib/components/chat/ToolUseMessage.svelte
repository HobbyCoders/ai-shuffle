<script lang="ts">
	/**
	 * ToolUseMessage - Collapsible tool execution display
	 */
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import ToolScreenshot from './ToolScreenshot.svelte';

	interface Props {
		name: string;
		input?: Record<string, unknown>;
		partialInput?: string;
		result?: string;
		status: 'running' | 'complete' | 'error';
		streaming?: boolean;
		serverSide?: boolean;
		/** Data URI of a screenshot from MCP image results (e.g. Claude-in-Chrome), shown inline below the tool call. */
		screenshot?: string;
	}

	let { name, input, partialInput, result, status, streaming = false, serverSide = false, screenshot }: Props = $props();

	// Chat preferences (reactive via .current getter)
	const prefs = $derived(chatPreferences.current);

	// Track if user has manually toggled this details element
	// Once toggled, user's choice takes precedence over preference
	let userToggled = $state(false);
	let detailsOpen = $state(false);

	// Sync from preferences when user hasn't manually toggled
	$effect(() => {
		if (!userToggled) {
			detailsOpen = prefs.toolsStartExpanded;
		}
	});

	function handleToggle() {
		userToggled = true;
	}

	// Extract field from partial JSON string using regex (for streaming)
	// Handles multiline content with escaped characters
	function extractFieldFromPartial(partial: string, fieldName: string): string {
		// Find the start of the field
		const fieldPattern = new RegExp(`"${fieldName}"\\s*:\\s*"`);
		const fieldMatch = partial.match(fieldPattern);
		if (!fieldMatch || fieldMatch.index === undefined) {
			return '';
		}

		// Find the start of the string value (after the opening quote)
		const valueStart = fieldMatch.index + fieldMatch[0].length;

		// Manually parse the JSON string, handling escapes
		let result = '';
		let i = valueStart;
		while (i < partial.length) {
			const char = partial[i];
			if (char === '\\' && i + 1 < partial.length) {
				// Handle escape sequences
				const nextChar = partial[i + 1];
				switch (nextChar) {
					case 'n': result += '\n'; break;
					case 'r': result += '\r'; break;
					case 't': result += '\t'; break;
					case '"': result += '"'; break;
					case '\\': result += '\\'; break;
					default: result += nextChar; break;
				}
				i += 2;
			} else if (char === '"') {
				// End of string
				break;
			} else {
				result += char;
				i++;
			}
		}

		return result;
	}

	// Tool icon categories - returns icon type for SVG rendering
	type IconType = 'image' | 'video' | 'cube' | 'file' | 'pencil' | 'search' | 'terminal' | 'globe' | 'bot' | 'list' | 'cog' | 'clock' | 'bell' | 'eye' | 'branch' | 'stop' | 'send';

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
		Glob: 'search', Grep: 'search', ToolSearch: 'search',
		// Terminal
		Bash: 'terminal', KillShell: 'terminal', SlashCommand: 'terminal', LSP: 'terminal',
		// Web
		WebFetch: 'globe', WebSearch: 'globe',
		// Agent / Skills
		Task: 'bot', Skill: 'bot', Agent: 'bot',
		// Todo
		TodoWrite: 'list',
		// Plan mode / Questions
		EnterPlanMode: 'list', ExitPlanMode: 'list', AskUserQuestion: 'bot',
		// Scheduling
		ScheduleWakeup: 'clock', CronCreate: 'clock', CronList: 'clock', CronDelete: 'clock',
		RemoteTrigger: 'send',
		// Worktree
		EnterWorktree: 'branch', ExitWorktree: 'branch',
		// Monitor / background tasks
		Monitor: 'eye', TaskOutput: 'eye', TaskStop: 'stop',
		// Notifications
		PushNotification: 'bell',
	};

	function getToolIconType(toolName: string): IconType {
		// Direct lookup first
		if (TOOL_ICON_MAP[toolName]) return TOOL_ICON_MAP[toolName];
		// Handle MCP-prefixed names (e.g., mcp__browser-tools__browser_navigate → browser_navigate)
		const mcpSuffix = toolName.includes('__') ? toolName.split('__').pop() : undefined;
		if (mcpSuffix && TOOL_ICON_MAP[mcpSuffix]) return TOOL_ICON_MAP[mcpSuffix];
		return 'cog';
	}

	function getToolSummary(toolInput?: Record<string, unknown>): string {
		if (!toolInput) return '';
		// Use the description field that tools provide, fallback to path/pattern fields
		return String(
			toolInput.description ||
			toolInput.url ||  // Browser tools - show URL
			toolInput.skill ||  // Skill tool - show skill name
			toolInput.file_path ||
			toolInput.path ||
			toolInput.notebook_path ||
			toolInput.pattern ||
			toolInput.glob ||
			toolInput.query ||
			toolInput.command ||
			toolInput.prompt ||
			toolInput.text ||  // Browser type tool
			toolInput.selector ||
			toolInput.label ||  // Browser fill tool
			toolInput.script ||  // Browser evaluate tool
			''
		);
	}

	// Extract summary from partial JSON during streaming
	function getPartialSummary(partial: string): string {
		// Try to parse as complete JSON first
		try {
			const parsed = JSON.parse(partial);
			return getToolSummary(parsed);
		} catch {
			// Fallback: extract fields via regex from incomplete JSON (in priority order)
			const fields = ['description', 'skill', 'file_path', 'path', 'notebook_path', 'pattern', 'glob', 'query', 'command', 'prompt'];
			for (const field of fields) {
				const value = extractFieldFromPartial(partial, field);
				if (value) return value;
			}
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
				data.audio_url ||
				data.audio_base64 ||
				data.file_url
			);
		} catch {
			return false;
		}
	}

	// Short label for the generated-media kind ("Image" | "Video" | "Audio").
	function toolResultMediaLabel(content: string): string {
		try {
			const data = JSON.parse(content);
			if (data.video_url) return 'Video';
			if (data.audio_url || data.audio_base64) return 'Audio';
			return 'Image';
		} catch {
			return 'Image';
		}
	}

	// Extract media URL from tool result JSON for rendering
	function getMediaFromResult(content: string): { type: 'image' | 'video' | 'audio'; src: string; filename?: string } | null {
		try {
			const data = JSON.parse(content);
			if (!data.success) return null;
			if (data.video_url) return { type: 'video', src: data.video_url, filename: data.filename };
			if (data.audio_url) return { type: 'audio', src: data.audio_url, filename: data.filename };
			if (data.image_url) return { type: 'image', src: data.image_url, filename: data.filename };
			if (data.image_base64) {
				const mime = data.mime_type || 'image/png';
				return { type: 'image', src: `data:${mime};base64,${data.image_base64}`, filename: data.filename };
			}
			if (data.audio_base64) {
				const mime = data.mime_type || 'audio/mpeg';
				return { type: 'audio', src: `data:${mime};base64,${data.audio_base64}`, filename: data.filename };
			}
			return null;
		} catch {
			return null;
		}
	}
</script>

<div class="w-full tool-use-message">
	<div class="min-w-0">
		<details class="w-full border border-border rounded-lg overflow-hidden group" bind:open={detailsOpen} ontoggle={handleToggle}>
			<summary class="tool-summary w-full px-3 py-2 flex items-center gap-2 cursor-pointer list-none transition-colors">
				<!-- Status indicator - use toolStatus as primary, not streaming -->
				{#if status === 'running'}
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
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
						<circle cx="8.5" cy="8.5" r="1.5"/>
						<polyline points="21 15 16 10 5 21"/>
					</svg>
				{:else if getToolIconType(name) === 'video'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<polygon points="23 7 16 12 23 17 23 7"/>
						<rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
					</svg>
				{:else if getToolIconType(name) === 'cube'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
						<polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
						<line x1="12" y1="22.08" x2="12" y2="12"/>
					</svg>
				{:else if getToolIconType(name) === 'file'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
						<polyline points="14 2 14 8 20 8"/>
						<line x1="16" y1="13" x2="8" y2="13"/>
						<line x1="16" y1="17" x2="8" y2="17"/>
					</svg>
				{:else if getToolIconType(name) === 'pencil'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>
					</svg>
				{:else if getToolIconType(name) === 'search'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="11" cy="11" r="8"/>
						<line x1="21" y1="21" x2="16.65" y2="16.65"/>
					</svg>
				{:else if getToolIconType(name) === 'terminal'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<polyline points="4 17 10 11 4 5"/>
						<line x1="12" y1="19" x2="20" y2="19"/>
					</svg>
				{:else if getToolIconType(name) === 'globe'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="12" cy="12" r="10"/>
						<line x1="2" y1="12" x2="22" y2="12"/>
						<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
					</svg>
				{:else if getToolIconType(name) === 'bot'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<rect x="3" y="11" width="18" height="10" rx="2"/>
						<circle cx="12" cy="5" r="2"/>
						<path d="M12 7v4"/>
						<line x1="8" y1="16" x2="8" y2="16"/>
						<line x1="16" y1="16" x2="16" y2="16"/>
					</svg>
				{:else if getToolIconType(name) === 'list'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<line x1="8" y1="6" x2="21" y2="6"/>
						<line x1="8" y1="12" x2="21" y2="12"/>
						<line x1="8" y1="18" x2="21" y2="18"/>
						<line x1="3" y1="6" x2="3.01" y2="6"/>
						<line x1="3" y1="12" x2="3.01" y2="12"/>
						<line x1="3" y1="18" x2="3.01" y2="18"/>
					</svg>
				{:else if getToolIconType(name) === 'clock'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="12" cy="12" r="10"/>
						<polyline points="12 6 12 12 16 14"/>
					</svg>
				{:else if getToolIconType(name) === 'bell'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
						<path d="M13.73 21a2 2 0 0 1-3.46 0"/>
					</svg>
				{:else if getToolIconType(name) === 'eye'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
						<circle cx="12" cy="12" r="3"/>
					</svg>
				{:else if getToolIconType(name) === 'branch'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<line x1="6" y1="3" x2="6" y2="15"/>
						<circle cx="18" cy="6" r="3"/>
						<circle cx="6" cy="18" r="3"/>
						<path d="M18 9a9 9 0 0 1-9 9"/>
					</svg>
				{:else if getToolIconType(name) === 'stop'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="12" cy="12" r="10"/>
						<rect x="9" y="9" width="6" height="6"/>
					</svg>
				{:else if getToolIconType(name) === 'send'}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<line x1="22" y1="2" x2="11" y2="13"/>
						<polygon points="22 2 15 22 11 13 2 9 22 2"/>
					</svg>
				{:else}
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<circle cx="12" cy="12" r="3"/>
						<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
					</svg>
				{/if}
				<!-- Tool name -->
				<span class="text-sm font-medium text-foreground flex-shrink-0">{name}</span>
				<!-- Server-side badge -->
				{#if serverSide}
					<span
						class="text-[10px] font-medium px-1.5 py-0.5 rounded border border-primary/30 bg-primary/10 text-primary flex-shrink-0 uppercase tracking-wide"
						title="Executed server-side by Anthropic API"
					>server</span>
				{/if}
				<!-- Summary (if available) -->
				{#if summary}
					<span class="text-muted-foreground flex-shrink-0">&bull;</span>
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
			<div>
				{#if partialInput || input}
					<div class="px-3 py-2">
						<div class="grid grid-cols-1 gap-2">
							<!-- Input -->
							<div class="min-w-0">
								<div class="text-[10px] text-muted-foreground mb-1 font-medium">Input</div>
								<div class="rounded border border-zinc-500/20 bg-zinc-500/5 p-2">
									<div class="max-h-60 overflow-auto">
										{#if streaming && partialInput}
											<pre class="text-[10px] font-mono text-foreground/80 m-0 w-max !bg-transparent !p-0 !rounded-none">{partialInput}<span class="inline-block w-1.5 h-3 ml-0.5 bg-primary animate-pulse"></span></pre>
										{:else if input && Object.keys(input).length > 0}
											<pre class="text-[10px] font-mono text-foreground/80 m-0 w-max !bg-transparent !p-0 !rounded-none">{JSON.stringify(input, null, 2)}</pre>
										{:else if partialInput}
											<pre class="text-[10px] font-mono text-foreground/80 m-0 w-max !bg-transparent !p-0 !rounded-none">{partialInput}</pre>
										{/if}
									</div>
								</div>
							</div>
							<!-- Result -->
							<div class="min-w-0">
								<div class="text-[10px] text-muted-foreground mb-1 font-medium">Result</div>
								<div class="rounded border border-zinc-500/20 bg-zinc-500/5 p-2">
									<div class="max-h-60 overflow-auto">
										{#if result}
											{#if toolResultHasMedia(result)}
												<span class="text-[10px] text-green-500">&#10003; {toolResultMediaLabel(result)} generated</span>
											{:else}
												<pre class="text-[10px] font-mono text-foreground/80 m-0 w-max !bg-transparent !p-0 !rounded-none">{result}</pre>
											{/if}
										{:else if status === 'error'}
											<pre class="text-[10px] font-mono text-red-400 m-0 w-max !bg-transparent !p-0 !rounded-none">[Interrupted]</pre>
										{:else if status === 'running'}
											<pre class="text-[10px] font-mono text-muted-foreground m-0 w-max !bg-transparent !p-0 !rounded-none">Running...</pre>
										{:else}
											<pre class="text-[10px] font-mono text-muted-foreground m-0 w-max !bg-transparent !p-0 !rounded-none"> </pre>
										{/if}
									</div>
								</div>
							</div>
						</div>
					</div>
				{/if}
			</div>
		</details>
		<!-- Screenshot (e.g. Claude-in-Chrome) rendered directly below the tool
		     call, OUTSIDE the <details> so it shows regardless of expand/collapse
		     state. When this tool is grouped, ToolUseGroup omits the screenshot
		     here and hoists it below the whole group instead (so it isn't trapped
		     inside the collapsed group). -->
		{#if screenshot}
			<ToolScreenshot {screenshot} {name} />
		{/if}
	</div>
</div>

<style>
	.tool-summary:hover {
		background: var(--hover-overlay);
	}

	.tool-summary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
</style>
