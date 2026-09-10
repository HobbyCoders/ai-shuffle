<script lang="ts">
	import type { ChatMessage } from '$lib/stores/types';
	import { isSubagentRunning } from '$lib/utils/subagent-status';
	import { openSessionLink } from '$lib/services/session-navigation';

	let { message, runningToolIds }: {
		message: ChatMessage;
		runningToolIds?: Set<string>;
	} = $props();

	const statusInfo = $derived({
		isRunning: isSubagentRunning(message.agentStatus, message.toolId, runningToolIds),
		isError: message.agentStatus === 'error'
	});
	const statusLabel = $derived(statusInfo.isRunning ? 'Running' : statusInfo.isError ? 'Failed' : message.agentStatus === 'pending' ? 'Pending' : 'Completed');

	// Map known SDK agent types to display labels
	const KNOWN_AGENT_LABELS: Record<string, string> = {
		'general-purpose': 'General',
		'explore': 'Explore',
		'plan': 'Plan',
		'frontend-designer': 'Frontend',
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

</script>

<details class="w-full subagent-card group">
	<summary class="subagent-summary">
		<!-- Agent identity stays in the parent; activity lives in the transcript. -->
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
			<span class="sa-status" role="status">{statusLabel}</span>

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
			{:else if message.agentStatus === 'pending'}
				<svg class="sa-glyph" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9" /></svg>
			{:else}
				<svg class="sa-glyph is-ok" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="M20 6 9 17l-5-5" />
				</svg>
			{/if}
		</span>
	</summary>

	<div class="sa-body">
		{#if message.agentSessionId}
			<a class="transcript-link" href={`/?session=${encodeURIComponent(message.agentSessionId)}`}
				onclick={(event) => openSessionLink(event, message.agentSessionId!)} data-sveltekit-reload>
				Open transcript <span aria-hidden="true">↗</span>
			</a>
		{:else}
			<p class="transcript-unavailable">{statusInfo.isRunning ? 'The transcript is not available yet.' : 'No separate transcript is available for this agent.'}</p>
		{/if}
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

	.subagent-summary:hover,
	.subagent-card[open] > .subagent-summary {
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

	.sa-status { font-size: 11px; color: var(--muted-foreground); }
	.transcript-link { display: inline-flex; align-items: center; gap: 6px; padding: 5px 0; color: var(--primary); font-size: 11px; text-decoration: none; }
	.transcript-link:hover { text-decoration: underline; }
	.transcript-link:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.transcript-unavailable { padding: 10px 0; margin: 0; color: var(--muted-foreground); font-size: 12px; }
	@media (max-width: 639px) {
		.sa-chip { display: none; }
		.sa-name { flex-shrink: 0; max-width: 45%; }
		.transcript-link { min-height: 44px; }
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

</style>
