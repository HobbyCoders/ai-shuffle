<script lang="ts">
	import type { AutomationRun } from '$lib/api/automations';
	import { slide } from 'svelte/transition';
	import {
		Loader2,
		ChevronDown,
		ChevronRight
	} from 'lucide-svelte';

	interface Props {
		runs: AutomationRun[];
		runsLoading: boolean;
		expandedRunIds: Set<string>;
		onToggleRunExpanded: (runId: string) => void;
		formatTime: (dateStr: string | null) => string;
		formatCost: (cost: number) => string;
		formatDuration: (ms: number) => string;
		getRunStatusBadgeClass: (status: AutomationRun['status']) => string;
	}

	let {
		runs,
		runsLoading,
		expandedRunIds,
		onToggleRunExpanded,
		formatTime,
		formatCost,
		formatDuration,
		getRunStatusBadgeClass
	}: Props = $props();
</script>

<div class="auto-runs-section">
	<h4 class="auto-section-title">Run History</h4>
	{#if runsLoading}
		<div class="auto-loading" style="padding: 24px">
			<Loader2 size={20} class="animate-spin" />
			<p>Loading runs...</p>
		</div>
	{:else if runs.length === 0}
		<p class="auto-runs-empty">No runs yet</p>
	{:else}
		<div class="auto-runs-list">
			{#each runs as run (run.id)}
				<div class="auto-run-item">
					<button class="auto-run-row" onclick={() => onToggleRunExpanded(run.id)}>
						{#if expandedRunIds.has(run.id)}
							<ChevronDown size={14} />
						{:else}
							<ChevronRight size={14} />
						{/if}
						<span class="card-badge {getRunStatusBadgeClass(run.status)}">{run.status}</span>
						<span class="auto-run-meta">
							{run.trigger_data?.source || 'manual'}
						</span>
						<span class="auto-run-meta">{formatCost(run.cost_usd)}</span>
						<span class="auto-run-meta">{formatDuration(run.duration_ms)}</span>
						<span class="auto-run-meta auto-run-time">{formatTime(run.created_at)}</span>
					</button>
					{#if expandedRunIds.has(run.id)}
						<div class="auto-run-expanded" transition:slide={{ duration: 150 }}>
							{#if run.response}
								<div class="auto-run-response">
									<span class="card-form-label">Response</span>
									<pre class="auto-run-pre">{run.response}</pre>
								</div>
							{/if}
							{#if run.error}
								<div class="auto-run-error">
									<span class="card-form-label">Error</span>
									<pre class="auto-run-pre auto-run-pre--error">{run.error}</pre>
								</div>
							{/if}
							<div class="auto-run-details-grid">
								<span>Tokens in: {run.tokens_in}</span>
								<span>Tokens out: {run.tokens_out}</span>
								<span>Output: {run.output_status}</span>
								{#if run.session_id}
									<span>Session: {run.session_id.slice(0, 8)}...</span>
								{/if}
							</div>
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.auto-runs-section {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
	}

	.auto-section-title {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
		margin: 0 0 12px 0;
	}

	.auto-loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px;
		color: var(--muted-foreground);
		gap: 12px;
	}

	.auto-loading :global(svg) {
		color: var(--primary);
	}

	.auto-runs-empty {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-tertiary, var(--muted-foreground));
		text-align: center;
		padding: 24px;
	}

	.auto-runs-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.auto-run-item {
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-sm, calc(var(--radius, 0.75rem) * 0.5));
		overflow: hidden;
	}

	.auto-run-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		background: var(--surface-1);
		border: none;
		color: inherit;
		cursor: pointer;
		width: 100%;
		text-align: left;
		font-size: var(--text-sm, 0.75rem);
		transition: background 100ms ease;
	}

	.auto-run-row:hover {
		background: var(--surface-hover, var(--muted));
	}

	.auto-run-meta {
		color: var(--text-secondary, var(--muted-foreground));
		font-size: var(--text-xs, 0.6875rem);
		white-space: nowrap;
	}

	.auto-run-time {
		margin-left: auto;
	}

	.auto-run-expanded {
		padding: 12px;
		border-top: 1px solid var(--border-subtle, var(--border));
		background: var(--surface-0, var(--card));
	}

	.auto-run-response,
	.auto-run-error {
		margin-bottom: 8px;
	}

	.auto-run-pre {
		padding: 8px 12px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-sm, calc(var(--radius, 0.75rem) * 0.5));
		font-size: var(--text-xs, 0.6875rem);
		font-family: var(--card-font-mono, monospace);
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 200px;
		overflow-y: auto;
		margin: 4px 0 0 0;
		color: var(--text-primary, var(--foreground));
	}

	.auto-run-pre--error {
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 25%, transparent);
		color: var(--destructive);
	}

	.auto-run-details-grid {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, var(--muted-foreground));
	}
</style>
