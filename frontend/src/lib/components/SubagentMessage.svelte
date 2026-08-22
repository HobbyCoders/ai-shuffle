<script lang="ts">
	/**
	 * SubagentMessage - Display for subagent work (matching tool message group style)
	 */
	import type { ChatMessage } from '$lib/stores/types';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { extractToolResultMedia, mediaHtml } from '$lib/utils/media';
	import { isSubagentRunning } from '$lib/utils/subagent-status';
	import ToolScreenshot from '$lib/components/chat/ToolScreenshot.svelte';
	import ToolIcon from '$lib/components/chat/ToolIcon.svelte';
	import { displayToolName } from '$lib/utils/tool-icons';

	interface Props {
		message: ChatMessage;
		/** Launch tool_use ids of background tasks the registry reports as currently running. */
		runningToolIds?: Set<string>;
	}

	let { message, runningToolIds }: Props = $props();

	// Code mode: a run_python cell that is running or succeeded is plumbing, not
	// work. The tool calls it makes arrive as their own child rows, so showing
	// the cell adds a wall of Python and inflates the done/total counter. A
	// failed cell still renders - that one is a problem worth reading.
	const isHiddenCodeModeCell = (c: { toolName?: string; toolStatus?: string }) =>
		c.toolName === 'run_python' && c.toolStatus !== 'error';

	// Filter valid children (ensure they have required id field)
	const validChildren = $derived(
		(message.agentChildren || []).filter(c => c && c.id && !isHiddenCodeModeCell(c))
	);

	// Count children by type — the collapsed header shows a completed/total
	// ratio only.
	const childCounts = $derived.by(() => {
		const children = validChildren;
		const toolUseChildren = children.filter(c => c.type === 'tool_use');
		const completedTools = toolUseChildren.filter(c => c.toolStatus === 'complete').length;

		return {
			total: children.length,
			toolUse: toolUseChildren.length,
			completedTools
		};
	});

	// Get status display. `isRunning` also honors the registry truth of running
	// background tasks (activeTasks), so a subagent card whose launch tool id is
	// still running is forced to the spinner even if the server-supplied status
	// is stale (e.g. fresh loads of parked sessions).
	const statusInfo = $derived({
		isRunning: isSubagentRunning(message.agentStatus, message.toolId, runningToolIds),
		isError: message.agentStatus === 'error'
	});

	// Map known SDK agent types to display labels
	const KNOWN_AGENT_LABELS: Record<string, string> = {
		'general-purpose': 'General',
		'explore': 'Explore',
		'plan': 'Plan',
		'frontend-designer': 'Frontend',
		'chat-history-summarizer': 'Summarizer',
		'statusline-setup': 'Status Line',
		'plugin-dev:create-plugin': 'Plugin',
		'plugin-dev:agent-creator': 'Agent Creator',
		'plugin-dev:plugin-validator': 'Validator',
		'plugin-dev:skill-reviewer': 'Skill Review',
		'plugin-dev:hook-development': 'Hook Dev',
		'plugin-dev:mcp-integration': 'MCP',
		'plugin-dev:plugin-structure': 'Plugin',
		'plugin-dev:skill-development': 'Skill Dev',
		'plugin-dev:command-development': 'Command Dev',
		'plugin-dev:plugin-settings': 'Settings',
		'skill-creator:skill-creator': 'Skill Creator',
	};

	function getModelBadge(agentType?: string): { label: string; color: string } {
		if (!agentType || agentType === 'unknown') {
			return { label: 'Agent', color: 'bg-primary/20 text-primary' };
		}
		const key = agentType.toLowerCase();
		const knownLabel = KNOWN_AGENT_LABELS[key];
		if (knownLabel) {
			return { label: knownLabel, color: 'bg-primary/20 text-primary' };
		}
		return { label: agentType, color: 'bg-primary/20 text-primary' };
	}

	// Format usage stats from task_progress updates
	function formatUsage(usage?: ChatMessage['agentUsage']): string | null {
		if (!usage) return null;
		const parts: string[] = [];
		if (usage.tool_uses) parts.push(`${usage.tool_uses} tools`);
		// ProviderAgent workers report raw input/output counts and no
		// `total_tokens`, which used to render as nothing at all.
		const totalTokens =
			usage.total_tokens || (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
		if (totalTokens) {
			const k = totalTokens >= 1000
				? `${(totalTokens / 1000).toFixed(0)}k`
				: String(totalTokens);
			parts.push(`${k} tokens`);
		}
		if (usage.duration_ms && usage.duration_ms > 1000) {
			const secs = Math.round(usage.duration_ms / 1000);
			if (secs >= 60) {
				const mins = Math.floor(secs / 60);
				const rem = secs % 60;
				parts.push(rem > 0 ? `${mins}m ${rem}s` : `${mins}m`);
			} else {
				parts.push(`${secs}s`);
			}
		}
		return parts.length > 0 ? parts.join(' · ') : null;
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

	/**
	 * Extract a short summary from tool input for the collapsed header line.
	 * Tries complete input first, falls back to partial JSON regex extraction.
	 */
	function getChildSummary(child: import('$lib/stores/types').SubagentChildMessage): string {
		// Try complete input first
		if (child.toolInput) {
			const input = child.toolInput;
			// Note/Skill-style rows carry their argument under other keys; without
			// them those child rows rendered as a bare tool name, no preview.
			const candidates = ['description', 'file_path', 'path', 'pattern', 'command', 'query', 'prompt', 'url', 'skill', 'glob', 'note', 'content', 'text', 'name'];
			for (const key of candidates) {
				if (input[key]) {
					const val = String(input[key]);
					return val.length > 80 ? val.slice(0, 80) + '\u2026' : val;
				}
			}
			return '';
		}
		// Fall back to partial JSON extraction during streaming
		if (child.partialToolInput) {
			// Note/Skill-style rows carry their argument under other keys; without
			// them those child rows rendered as a bare tool name, no preview.
			const candidates = ['description', 'file_path', 'path', 'pattern', 'command', 'query', 'prompt', 'url', 'skill', 'glob', 'note', 'content', 'text', 'name'];
			for (const key of candidates) {
				const regex = new RegExp(`"${key}"\\s*:\\s*"([^"]*)"?`);
				const match = child.partialToolInput.match(regex);
				if (match?.[1]) {
					const val = match[1];
					return val.length > 80 ? val.slice(0, 80) + '\u2026' : val;
				}
			}
		}
		return '';
	}

	function truncateContent(content: string, maxLength: number = 500): string {
		if (content.length <= maxLength) return content;
		return content.substring(0, maxLength) + '...';
	}

	/**
	 * Split a worker result that is really a payload object into
	 * `{ message, rest }`.
	 *
	 * A failed worker's result arrives as a JSON object — or, from the Python
	 * side, a dict repr with single quotes — whose `message`/`error` field is the
	 * only part a human needs; everything else (status codes, rate-limit headers)
	 * is noise that used to be dumped verbatim into a <pre>. Returns `null` when
	 * the content is ordinary prose, which is the common case.
	 *
	 * Deliberately regex-based and dependency-free: a dict repr is not JSON, and
	 * pulling in a parser for a display nicety is not worth it.
	 */
	function splitResultPayload(content: string): { message: string; rest: string } | null {
		const text = (content ?? '').trim();
		if (!(text.startsWith('{') || text.includes("'message'") || text.includes('"message"')))
			return null;
		const match = /['"](?:message|error_message)['"]\s*:\s*['"]([\s\S]+?)['"]\s*[,}]/.exec(text);
		if (!match?.[1]) return null;
		const message = match[1].replace(/\\n/g, '\n').trim();
		if (!message) return null;
		const rest = (text.slice(0, match.index) + text.slice(match.index + match[0].length))
			.replace(/\s+/g, ' ')
			.trim();
		return { message, rest };
	}

	// Check if a tool result contains embeddable media (image/video/audio)
	function toolResultHasMedia(content: string): boolean {
		return extractToolResultMedia(content) !== null;
	}

	// Render tool result media (image/video/audio) inline using the shared
	// media renderer, so subagent output matches the main chat card.
	function renderToolResult(content: string): string {
		const media = extractToolResultMedia(content);
		if (!media) return '';
		const label = media.kind.charAt(0).toUpperCase() + media.kind.slice(1);
		return `<div class="mb-2 text-xs text-green-500">✓ ${label} generated successfully</div>` +
			mediaHtml(media.kind, media.src, '', media.filename);
	}
</script>

<details class="w-full subagent-card group">
	<summary class="subagent-summary">
		<!-- Agent glyph — a subagent reads as one more tool row, just expandable
		     into its nested tools and responses. -->
		<svg class="sa-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
			<rect width="16" height="11" x="4" y="9" rx="2" />
			<path d="M12 5v4" />
			<circle cx="12" cy="4" r="1.4" />
			<path d="M9 14h.01" />
			<path d="M15 14h.01" />
		</svg>

		<!-- IDENTITY: the AGENT's name when the dispatcher supplied one
		     (2026-08-17 ruling), otherwise the historical model/type badge. -->
		{#if message.agentName}
			<span class="sa-name">{message.agentName}</span>
			<!-- The model slug stays visible, demoted to a secondary chip. -->
			{#if message.agentType}
				<span class="sa-chip" title="Model: {message.agentType}">
					{getModelBadge(message.agentType).label}
				</span>
			{/if}
		{:else if message.agentType}
			<span class="sa-name">{getModelBadge(message.agentType).label}</span>
		{/if}

		<!-- Description or default label -->
		<span class="sa-desc">{message.agentDescription || 'Subagent'}</span>

		<span class="sa-right">
			<!-- Usage stats from task_progress (tokens, duration) -->
			{#if message.agentUsage}
				{@const usageText = formatUsage(message.agentUsage)}
				{#if usageText}
					<span class="sa-usage mono">{usageText}</span>
				{/if}
			{/if}

			<!-- Tool count. Failures are NOT spelled out here: the extra
			     "(n failed)" text overflowed the summary row on phones and put a
			     horizontal scrollbar on the whole chat. The red X glyph and the
			     completed/total ratio still carry the signal. -->
			{#if childCounts.toolUse > 0}
				<span class="sa-count mono">{childCounts.completedTools}/{childCounts.toolUse}</span>
			{/if}

			<svg class="sa-chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m6 9 6 6 6-6" />
			</svg>

			{#if statusInfo.isRunning}
				<svg class="sa-glyph is-running" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
					<path d="M12 3a9 9 0 1 0 9 9" />
				</svg>
			{:else if statusInfo.isError}
				<svg class="sa-glyph is-error" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
					<path d="M18 6 6 18M6 6l12 12" />
				</svg>
			{:else}
				<svg class="sa-glyph is-ok" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="M20 6 9 17l-5-5" />
				</svg>
			{/if}
		</span>
	</summary>

	<!-- Expanded content: indented past the icon gutter like every other
	     expanded tool row -->
	<div class="sa-body">
		<div class="max-h-[32rem] overflow-y-auto">
			<!-- Initial prompt section -->
			{#if message.agentPrompt}
				<div class="px-4 py-3 border-b border-border/50">
					<div class="text-xs text-muted-foreground mb-1 font-medium">Prompt</div>
					<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{message.agentPrompt}</pre>
				</div>
			{/if}

			<!-- Agent children (tool calls and text messages) -->
			{#if validChildren.length > 0}
				{#each validChildren as child (child.id)}
					{#if child.type === 'tool_use'}
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<details class="group/tool">
								<summary class="subagent-tool-summary flex items-center gap-2 cursor-pointer list-none -mx-1 px-1 py-1 rounded">
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
									<!-- Per-tool icon, same mapping as the main feed (every child
									     row used to draw one hard-coded cog). -->
									<ToolIcon name={child.toolName || ''} />
									<span class="text-sm font-medium text-foreground" title={child.toolName || 'Tool'}>{displayToolName(child.toolName || '') || 'Tool'}</span>
									{#if getChildSummary(child)}
										<span class="text-sm text-muted-foreground truncate min-w-0">{getChildSummary(child)}</span>
									{/if}
									<svg class="w-4 h-4 text-muted-foreground ml-auto flex-shrink-0 transition-transform group-open/tool:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
									</svg>
								</summary>
								<div class="mt-2 space-y-2">
									{#if child.toolInput}
										<div class="p-2 bg-muted/50 rounded">
											<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Input</div>
											<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{formatToolInput(child.toolInput)}</pre>
										</div>
									{:else if child.partialToolInput}
										<div class="p-2 bg-muted/50 rounded">
											<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Input</div>
											<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{child.partialToolInput}<span class="inline-block w-1.5 h-3 ml-0.5 bg-primary animate-pulse"></span></pre>
										</div>
									{/if}
									{#if child.toolResult}
										<div class="p-2 bg-muted/50 rounded">
											<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Result</div>
											{#if toolResultHasMedia(child.toolResult)}
												{@html sanitizeHtml(renderToolResult(child.toolResult))}
											{:else}
												<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{truncateContent(child.toolResult)}</pre>
											{/if}
										</div>
									{/if}
								</div>
							</details>
							<!-- Screenshot sits OUTSIDE the collapsible details so it's
							     visible regardless of expand state (matches main lane). -->
							{#if child.toolScreenshot}
								<ToolScreenshot screenshot={child.toolScreenshot} name={child.toolName || 'Tool'} />
							{/if}
						</div>
					{:else if child.type === 'tool_result'}
						<!-- Standalone tool result (fallback) -->
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<details class="group/result">
								<summary class="subagent-tool-summary flex items-center gap-2 cursor-pointer list-none -mx-1 px-1 py-1 rounded">
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
									{#if toolResultHasMedia(child.content)}
										<div class="mt-2 p-2 bg-muted/50 rounded">
											{@html sanitizeHtml(renderToolResult(child.content))}
										</div>
									{:else}
										<pre class="mt-2 p-2 bg-muted/50 rounded text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{truncateContent(child.content)}</pre>
									{/if}
								{/if}
							</details>
							{#if child.toolScreenshot}
								<ToolScreenshot screenshot={child.toolScreenshot} name={child.toolName || 'Tool'} />
							{/if}
						</div>
					{:else if child.type === 'text'}
						<!-- Text child (subagent's text output) -->
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<div class="flex items-start gap-2">
								<svg class="w-4 h-4 text-primary flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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

			<!-- Final result section. A FAILED worker's result gets error chrome
			     (the header read exactly like a success before), and a payload
			     object is shown message-first with the raw dict collapsed. -->
			{#if message.content && message.agentStatus !== 'running'}
				{@const payload = statusInfo.isError ? splitResultPayload(message.content) : null}
				<div
					class="px-4 py-3 border-t {statusInfo.isError
						? 'border-red-500/40 bg-red-500/5'
						: 'border-border/50'}"
				>
					<div
						class="text-xs mb-1 font-medium {statusInfo.isError
							? 'text-red-500'
							: 'text-muted-foreground'}"
					>
						{statusInfo.isError ? 'Error' : 'Result'}
					</div>
					{#if toolResultHasMedia(message.content)}
						{@html sanitizeHtml(renderToolResult(message.content))}
					{:else if payload}
						<div class="text-sm text-foreground whitespace-pre-wrap break-words">
							{payload.message}
						</div>
						{#if payload.rest}
							<details class="mt-2">
								<summary class="text-xs text-muted-foreground cursor-pointer list-none">
									Show raw error payload
								</summary>
								<pre class="mt-1 text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{payload.rest}</pre>
							</details>
						{/if}
					{:else}
						<pre
							class="text-xs overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono {statusInfo.isError
								? 'text-foreground'
								: 'text-muted-foreground'}"
						>{message.content}</pre>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</details>

<style>
	.subagent-card {
		background: transparent;
	}

	@media (max-width: 639px) {
		.subagent-summary {
			min-height: 44px;
		}
	}

	.subagent-summary {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		min-width: 0;
		padding: 6px 8px;
		border-radius: 8px;
		cursor: pointer;
		list-style: none;
		overflow: hidden;
		transition: background-color 0.13s ease;
	}

	.sa-body {
		margin: 2px 0 0 15px;
		border-left: 1px solid var(--border);
		padding-left: 10px;
	}

	.subagent-summary::-webkit-details-marker {
		display: none;
	}

	.subagent-summary:hover {
		background: var(--scrim);
	}

	.subagent-summary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.sa-icon {
		flex: none;
		color: var(--subtle-foreground);
	}

	.sa-name {
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		font-size: 12.5px;
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
	}

	.sa-chip {
		flex: none;
		font-size: 10px;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--subtle-foreground);
		border: 1px solid var(--border);
		border-radius: 5px;
		padding: 1px 5px;
	}

	.sa-desc {
		min-width: 0;
		font-size: 12px;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.sa-right {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 8px;
		flex: none;
		color: var(--subtle-foreground);
	}

	.sa-usage,
	.sa-count {
		font-family: var(--font-mono);
		font-size: 11px;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.sa-usage {
		display: none;
	}

	@media (min-width: 640px) {
		.sa-usage { display: inline; }
	}

	.sa-chev {
		transition: transform 0.16s ease;
	}

	.subagent-card[open] .sa-chev {
		transform: rotate(180deg);
	}

	.sa-glyph.is-ok { color: var(--emerald); }
	.sa-glyph.is-error { color: var(--rose); }
	.sa-glyph.is-running {
		color: var(--primary);
		animation: sa-spin 0.9s linear infinite;
	}

	@keyframes sa-spin {
		to { transform: rotate(360deg); }
	}

	@media (prefers-reduced-motion: reduce) {
		.sa-glyph.is-running { animation-duration: 2.4s; }
	}

	.subagent-tool-summary:hover {
		background: var(--hover-overlay);
	}

	.subagent-tool-summary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
</style>
