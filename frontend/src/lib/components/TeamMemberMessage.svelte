<script lang="ts">
	/**
	 * TeamMemberMessage - Display for a single teammate in an agent team.
	 * Mirrors SubagentMessage patterns but surfaces team/teammate identity,
	 * teammate status, and a Kill action when a PID is known.
	 */
	import type { ChatMessage, TeammateStatus } from '$lib/stores/types';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { killTeammate } from '$lib/api/client';
	import ToolUseMessage from '$lib/components/chat/ToolUseMessage.svelte';

	/** Child toolStatus values are trusted — they only arrive as these three. */
	type ChildStatus = 'running' | 'complete' | 'error';

	interface Props {
		message: ChatMessage;
		parentSessionId: string;
	}

	let { message, parentSessionId }: Props = $props();

	let killing = $state(false);
	let killError = $state<string | null>(null);

	// Code mode: a run_python cell that is running or succeeded is plumbing, not
	// work. The tool calls it makes arrive as their own child rows, so showing
	// the cell adds a wall of Python and inflates the done/total counter. A
	// failed cell still renders - that one is a problem worth reading.
	const isHiddenCodeModeCell = (c: { toolName?: string; toolStatus?: string }) =>
		c.toolName === 'run_python' && c.toolStatus !== 'error';

	const validChildren = $derived(
		(message.agentChildren || []).filter((c) => c && c.id && !isHiddenCodeModeCell(c))
	);

	const childCounts = $derived.by(() => {
		const children = validChildren;
		const toolUseChildren = children.filter((c) => c.type === 'tool_use');
		const completedTools = toolUseChildren.filter((c) => c.toolStatus === 'complete').length;
		return {
			total: children.length,
			toolUse: toolUseChildren.length,
			completedTools
		};
	});

	const status: TeammateStatus = $derived(message.teammateStatus ?? 'running');

	const pillStyle = $derived.by(() => {
		switch (status) {
			case 'running':
				return 'bg-primary/20 text-primary';
			case 'completed':
				return 'bg-green-500/20 text-green-600';
			case 'error':
				return 'bg-red-500/20 text-red-600';
			case 'killed':
				return 'bg-amber-500/20 text-amber-600';
		}
	});

	const pillLabel = $derived(
		`${message.teamName ?? 'Team'} / ${message.teammateName ?? 'Teammate'}`
	);

	const canKill = $derived(
		status === 'running' && typeof message.teammatePid === 'number'
	);

	function truncateSessionId(id?: string): string {
		if (!id) return '';
		return id.length > 12 ? `${id.slice(0, 8)}\u2026${id.slice(-4)}` : id;
	}

	function formatUsage(usage?: ChatMessage['agentUsage']): string | null {
		if (!usage) return null;
		const parts: string[] = [];
		if (usage.tool_uses) parts.push(`${usage.tool_uses} tools`);
		if (usage.total_tokens) {
			const k =
				usage.total_tokens >= 1000
					? `${(usage.total_tokens / 1000).toFixed(0)}k`
					: String(usage.total_tokens);
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
		return parts.length > 0 ? parts.join(' \u00b7 ') : null;
	}

	function truncateContent(content: string, maxLength: number = 500): string {
		if (content.length <= maxLength) return content;
		return content.substring(0, maxLength) + '...';
	}

	function toolResultHasImage(content: string): boolean {
		try {
			const data = JSON.parse(content);
			return data.success && data.image_base64 && data.mime_type;
		} catch {
			return false;
		}
	}

	function renderToolResult(content: string): string {
		try {
			const data = JSON.parse(content);
			if (data.success && data.image_base64 && data.mime_type) {
				const mimeType = data.mime_type || 'image/png';
				const dataUrl = `data:${mimeType};base64,${data.image_base64}`;
				return `<div class="generated-image-result">
					<div class="mb-2 text-xs text-green-500">Image generated successfully</div>
					<img src="${dataUrl}" alt="Generated image" class="max-w-full max-h-64 rounded-lg shadow-md border border-border" />
				</div>`;
			}
		} catch {
			// not JSON
		}
		return '';
	}

	async function handleKill(event: MouseEvent) {
		event.preventDefault();
		event.stopPropagation();
		if (!canKill || killing) return;
		if (!message.teamName || !message.teammateName) return;
		killing = true;
		killError = null;
		try {
			await killTeammate(parentSessionId, message.teamName, message.teammateName);
		} catch (e) {
			killError = e instanceof Error ? e.message : 'Kill failed';
		} finally {
			killing = false;
		}
	}
</script>

<details class="w-full team-card group">
	<summary class="team-summary w-full min-w-0 px-4 py-2 flex items-center gap-2 cursor-pointer list-none transition-colors">
		<!-- Status icon -->
		{#if status === 'running'}
			<svg class="w-4 h-4 text-primary animate-spin shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
			</svg>
		{:else if status === 'completed'}
			<svg class="w-4 h-4 text-green-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
			</svg>
		{:else if status === 'error'}
			<svg class="w-4 h-4 text-red-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
			</svg>
		{:else if status === 'killed'}
			<svg class="w-4 h-4 text-amber-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<rect x="6" y="6" width="12" height="12" rx="1" stroke-width="2" />
			</svg>
		{/if}

		<!-- Team icon (people) -->
		<svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
		</svg>

		<!-- Team / teammate pill -->
		<span class="px-1.5 py-0.5 text-xs font-medium {pillStyle} rounded-full shrink-0 truncate max-w-[16rem]">
			{pillLabel}
		</span>

		<!-- Description or label -->
		<span class="text-sm font-medium text-foreground truncate min-w-0">
			{message.agentDescription || 'Teammate'}
		</span>

		<!-- Tool count -->
		{#if childCounts.toolUse > 0}
			<span class="text-muted-foreground shrink-0">•</span>
			<span class="text-xs text-muted-foreground shrink-0">
				{childCounts.completedTools}/{childCounts.toolUse} tools
			</span>
		{/if}

		<!-- Usage -->
		{#if message.agentUsage}
			{@const usageText = formatUsage(message.agentUsage)}
			{#if usageText}
				<span class="text-muted-foreground shrink-0">•</span>
				<span class="text-xs text-muted-foreground shrink-0">{usageText}</span>
			{/if}
		{/if}

		<div class="ml-auto flex items-center gap-2 shrink-0">
			{#if canKill}
				<button
					type="button"
					onclick={handleKill}
					disabled={killing}
					class="min-h-[32px] px-2.5 py-1 text-xs font-medium rounded-sm border border-red-500/40 text-red-600 hover:bg-red-500/10 active:bg-red-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
					aria-label="Kill teammate"
				>
					{killing ? 'Killing\u2026' : 'Kill'}
				</button>
			{/if}
			<!-- Chevron -->
			<svg class="w-4 h-4 text-muted-foreground transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
			</svg>
		</div>
	</summary>

	<!-- Expanded content -->
	<div class="team-body">
		<!-- Session id row -->
		{#if message.teammateSessionId}
			<div class="px-4 py-2 border-b border-border/50 flex items-center gap-2 text-xs">
				<span class="text-muted-foreground">Session:</span>
				<span class="font-mono text-muted-foreground truncate" title={message.teammateSessionId}>
					{truncateSessionId(message.teammateSessionId)}
				</span>
				{#if message.teammatePid !== undefined}
					<span class="text-muted-foreground">•</span>
					<span class="text-muted-foreground">pid <span class="font-mono">{message.teammatePid}</span></span>
				{/if}
			</div>
		{/if}

		{#if killError}
			<div class="px-4 py-2 border-b border-border/50 text-xs text-red-600">
				{killError}
			</div>
		{/if}

		<div class="max-h-128 overflow-y-auto">
			<!-- Initial prompt -->
			{#if message.agentPrompt}
				<div class="px-4 py-3 border-b border-border/50">
					<div class="text-xs text-muted-foreground mb-1 font-medium">Initial prompt</div>
					<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap wrap-break-word font-mono">{message.agentPrompt}</pre>
				</div>
			{/if}

			{#if validChildren.length > 0}
				{#each validChildren as child (child.id)}
					{#if child.type === 'tool_use'}
						<!-- Same work-log row the main feed and subagent cards use, so
						     nested tool calls read exactly like top-level ones. -->
						<ToolUseMessage
							name={child.toolName || 'Tool'}
							input={child.toolInput}
							partialInput={child.partialToolInput}
							result={child.toolResult ?? ''}
							status={(child.toolStatus as ChildStatus) || 'running'}
							screenshot={child.toolScreenshot}
						/>
					{:else if child.type === 'tool_result'}
						<!-- Standalone tool result (fallback) — no matching tool_use row
						     arrived, so render the payload as a plain muted block. -->
						<div class="team-orphan-result">
							<pre>{truncateContent(child.content)}</pre>
						</div>
						{#if child.toolScreenshot}
							<ToolUseMessage
								name={child.toolName || 'Tool'}
								result={child.content ?? ''}
								status="complete"
								screenshot={child.toolScreenshot}
							/>
						{/if}
					{:else if child.type === 'text'}
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<div class="flex items-start gap-2">
								<svg class="w-4 h-4 text-primary shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" />
								</svg>
								<div class="text-sm text-foreground whitespace-pre-wrap wrap-break-word">
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
				<div class="px-4 py-3 text-sm text-muted-foreground">No activity recorded</div>
			{/if}

			{#if message.content && status !== 'running'}
				<div class="px-4 py-3 border-t border-border/50">
					<div class="text-xs text-muted-foreground mb-1 font-medium">Result</div>
					{#if toolResultHasImage(message.content)}
						{@html sanitizeHtml(renderToolResult(message.content))}
					{:else}
						<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap wrap-break-word font-mono">{message.content}</pre>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</details>

<style>
	.team-card {
		min-width: 0;
		background: transparent;
	}

	.team-summary {
		padding: 6px 8px;
		border-radius: 8px;
		background: transparent;
	}
	.team-summary:hover {
		background: var(--scrim);
	}
	.team-summary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.team-body {
		margin: 2px 0 6px 14px;
		padding-left: 10px;
		border-left: 1px solid var(--border);
		overflow: hidden;
	}

	@media (max-width: 639px) {
		.team-summary {
			min-height: 44px;
		}

		.team-summary button {
			min-height: 36px;
		}
	}

	/* Orphan tool_result fallback (no matching tool_use row arrived). */
	.team-orphan-result {
		max-height: 15rem;
		overflow: auto;
		border-radius: 7px;
		border: 1px solid var(--border);
		background: var(--code-bg);
		padding: 7px 9px;
		margin-bottom: 6px;
	}

	.team-orphan-result pre {
		margin: 0;
		font-family: var(--font-mono);
		font-size: 11.5px;
		line-height: 1.6;
		color: var(--muted-foreground);
		white-space: pre-wrap;
		word-break: break-word;
	}
</style>
