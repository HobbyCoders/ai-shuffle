<script lang="ts">
	/**
	 * ToolUseMessage - Collapsible tool execution display
	 */
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import ToolScreenshot from './ToolScreenshot.svelte';
	import ToolIcon from './ToolIcon.svelte';
	import { displayToolName } from '$lib/utils/tool-icons';
	import { isDismissedQuestion, QUESTION_DISMISSED_LABEL } from './question-outcome';

	interface Props {
		name: string;
		input?: Record<string, unknown>;
		partialInput?: string;
		result?: string;
		status: 'running' | 'complete' | 'error';
		streaming?: boolean;
		serverSide?: boolean;
		/** Data URI of a screenshot from MCP image results (e.g. browser automation), shown inline below the tool call. */
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

	// Closing the AskUserQuestion modal resolves the tool call as a DENY (the
	// tool has no "unanswered" value). That is correct mechanically but it is
	// not a failure, so the card drops the red error chrome for neutral,
	// muted "dismissed" styling. See ./question-outcome.ts.
	const dismissedQuestion = $derived(isDismissedQuestion(name, status, result));

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
				{#if dismissedQuestion}
					<!-- Neutral "closed" mark: not an error, not a success. -->
					<svg class="w-4 h-4 text-muted-foreground flex-shrink-0" data-testid="tool-dismissed-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<circle cx="12" cy="12" r="9" stroke-width="2" />
						<path stroke-linecap="round" stroke-width="2" d="M8.5 12h7" />
					</svg>
				{:else if status === 'running'}
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
				<!-- Tool icon (shared mapping with the subagent card) -->
				<ToolIcon {name} />
				<!-- Tool name -->
				<span class="text-sm font-medium text-foreground flex-shrink-0" title={name}>{displayToolName(name)}</span>
				<!-- Server-side badge -->
				{#if serverSide}
					<span
						class="text-[10px] font-medium px-1.5 py-0.5 rounded border border-primary/30 bg-primary/10 text-primary flex-shrink-0 uppercase tracking-wide"
						title="Executed server-side by Anthropic API"
					>server</span>
				{/if}
				<!-- Dismissed question: say what actually happened, in muted chrome. -->
				{#if dismissedQuestion}
					<span
						class="text-[10px] font-medium px-1.5 py-0.5 rounded border border-border bg-muted text-muted-foreground flex-shrink-0 uppercase tracking-wide"
					>dismissed</span>
					<span class="text-muted-foreground flex-shrink-0">&bull;</span>
					<span
						class="text-sm text-muted-foreground truncate min-w-0"
						title={QUESTION_DISMISSED_LABEL}
					>{QUESTION_DISMISSED_LABEL}</span>
				<!-- Summary (if available) -->
				{:else if summary}
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
										{#if dismissedQuestion}
											<pre class="text-[10px] font-mono text-muted-foreground m-0 w-max !bg-transparent !p-0 !rounded-none" data-testid="tool-dismissed-result">{result}</pre>
										{:else if result}
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
		<!-- Screenshot (e.g. browser automation) rendered directly below the tool
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
