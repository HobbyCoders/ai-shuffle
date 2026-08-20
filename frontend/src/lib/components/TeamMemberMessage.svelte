<script lang="ts">
	/**
	 * TeamMemberMessage - Display for a single teammate in an agent team.
	 * Mirrors SubagentMessage patterns but surfaces team/teammate identity,
	 * teammate status, and a Kill action when a PID is known.
	 */
	import type { ChatMessage, SubagentChildMessage, TeammateStatus } from '$lib/stores/types';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { killTeammate } from '$lib/api/client';
	import ToolScreenshot from '$lib/components/chat/ToolScreenshot.svelte';

	interface Props {
		message: ChatMessage;
		parentSessionId: string;
	}

	let { message, parentSessionId }: Props = $props();

	let killing = $state(false);
	let killError = $state<string | null>(null);

	const validChildren = $derived(
		(message.agentChildren || []).filter((c) => c && c.id)
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

	function getChildSummary(child: SubagentChildMessage): string {
		const candidates = [
			'description',
			'file_path',
			'path',
			'pattern',
			'command',
			'query',
			'prompt',
			'url',
			'skill',
			'glob'
		];
		if (child.toolInput) {
			for (const key of candidates) {
				if (child.toolInput[key]) {
					const val = String(child.toolInput[key]);
					return val.length > 80 ? val.slice(0, 80) + '\u2026' : val;
				}
			}
			return '';
		}
		if (child.partialToolInput) {
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
			<svg class="w-4 h-4 text-primary animate-spin flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
			</svg>
		{:else if status === 'completed'}
			<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
			</svg>
		{:else if status === 'error'}
			<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
			</svg>
		{:else if status === 'killed'}
			<svg class="w-4 h-4 text-amber-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<rect x="6" y="6" width="12" height="12" rx="1" stroke-width="2" />
			</svg>
		{/if}

		<!-- Team icon (people) -->
		<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
		</svg>

		<!-- Team / teammate pill -->
		<span class="px-1.5 py-0.5 text-xs font-medium {pillStyle} rounded-full flex-shrink-0 truncate max-w-[16rem]">
			{pillLabel}
		</span>

		<!-- Description or label -->
		<span class="text-sm font-medium text-foreground truncate min-w-0">
			{message.agentDescription || 'Teammate'}
		</span>

		<!-- Tool count -->
		{#if childCounts.toolUse > 0}
			<span class="text-muted-foreground flex-shrink-0">•</span>
			<span class="text-xs text-muted-foreground flex-shrink-0">
				{childCounts.completedTools}/{childCounts.toolUse} tools
			</span>
		{/if}

		<!-- Usage -->
		{#if message.agentUsage}
			{@const usageText = formatUsage(message.agentUsage)}
			{#if usageText}
				<span class="text-muted-foreground flex-shrink-0">•</span>
				<span class="text-xs text-muted-foreground flex-shrink-0">{usageText}</span>
			{/if}
		{/if}

		<div class="ml-auto flex items-center gap-2 flex-shrink-0">
			{#if canKill}
				<button
					type="button"
					onclick={handleKill}
					disabled={killing}
					class="min-h-[32px] px-2.5 py-1 text-xs font-medium rounded border border-red-500/40 text-red-600 hover:bg-red-500/10 active:bg-red-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
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

		<div class="max-h-[32rem] overflow-y-auto">
			<!-- Initial prompt -->
			{#if message.agentPrompt}
				<div class="px-4 py-3 border-b border-border/50">
					<div class="text-xs text-muted-foreground mb-1 font-medium">Initial prompt</div>
					<pre class="text-xs text-muted-foreground overflow-x-auto max-h-32 whitespace-pre-wrap break-words font-mono">{message.agentPrompt}</pre>
				</div>
			{/if}

			{#if validChildren.length > 0}
				{#each validChildren as child (child.id)}
					{#if child.type === 'tool_use'}
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<details class="group/tool">
								<summary class="team-tool-summary flex items-center gap-2 cursor-pointer list-none -mx-1 px-1 py-1 rounded">
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
									<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
									</svg>
									<span class="text-sm font-medium text-foreground">{child.toolName || 'Tool'}</span>
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
											{#if toolResultHasImage(child.toolResult)}
												{@html sanitizeHtml(renderToolResult(child.toolResult))}
											{:else}
												<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{truncateContent(child.toolResult)}</pre>
											{/if}
										</div>
									{/if}
								</div>
							</details>
							{#if child.toolScreenshot}
								<ToolScreenshot screenshot={child.toolScreenshot} name={child.toolName || 'Tool'} />
							{/if}
						</div>
					{:else if child.type === 'tool_result'}
						<div class="px-4 py-2 border-b border-border/50 last:border-b-0">
							<details class="group/result">
								<summary class="team-tool-summary flex items-center gap-2 cursor-pointer list-none -mx-1 px-1 py-1 rounded">
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
				<div class="px-4 py-3 text-sm text-muted-foreground">No activity recorded</div>
			{/if}

			{#if message.content && status !== 'running'}
				<div class="px-4 py-3 border-t border-border/50">
					<div class="text-xs text-muted-foreground mb-1 font-medium">Result</div>
					{#if toolResultHasImage(message.content)}
						{@html sanitizeHtml(renderToolResult(message.content))}
					{:else}
						<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{message.content}</pre>
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
	.team-tool-summary:hover {
		background: var(--hover-overlay);
	}
	.team-tool-summary:focus-visible {
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
</style>
