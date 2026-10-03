<script lang="ts">
	import { automationModelLabel, type Automation, type AutomationRun } from '$lib/api/automations';
	import { fade, slide } from 'svelte/transition';
	import {
		ArrowLeft,
		Pencil,
		Play,
		Trash2
	} from 'lucide-svelte';
	import AutomationRunHistory from './AutomationRunHistory.svelte';

	interface Props {
		automation: Automation;
		runs: AutomationRun[];
		runsLoading: boolean;
		expandedRunIds: Set<string>;
		detailStats: { total_runs: number; total_cost_usd: number; successful_runs: number; failed_runs: number } | null;
		onBack: () => void;
		onEdit: (automation: Automation) => void;
		onTrigger: (id: string) => void;
		onDelete: (id: string) => void;
		onToggleRunExpanded: (runId: string) => void;
		formatTime: (dateStr: string | null) => string;
		formatCost: (cost: number) => string;
		formatDuration: (ms: number) => string;
		getStatusColor: (status: Automation['status']) => string;
		getRunStatusBadgeClass: (status: AutomationRun['status']) => string;
		triggerTypeLabel: (type: any) => string;
	}

	let {
		automation,
		runs,
		runsLoading,
		expandedRunIds,
		detailStats,
		onBack,
		onEdit,
		onTrigger,
		onDelete,
		onToggleRunExpanded,
		formatTime,
		formatCost,
		formatDuration,
		getStatusColor,
		getRunStatusBadgeClass,
		triggerTypeLabel
	}: Props = $props();

	function successRate(total: number, successful: number): string {
		if (total === 0) return 'N/A';
		return `${Math.round((successful / total) * 100)}%`;
	}
</script>

<div class="auto-detail" transition:fade={{ duration: 150 }}>
	<!-- Detail header -->
	<div class="auto-detail-header">
		<button class="card-back-btn" onclick={onBack} aria-label="Back to automations">
			<ArrowLeft size={16} />
		</button>
		<div class="auto-detail-header-info">
			<div class="auto-detail-name-row">
				<span
					class="auto-status-dot"
					style="background: {getStatusColor(automation.status)}"
				></span>
				<h3 class="auto-detail-name">{automation.name}</h3>
				{#each (automation.triggers || [{ type: automation.trigger_type }]) as trig}
					<span class="card-badge card-badge--muted">{triggerTypeLabel(trig.type)}</span>
				{/each}
				<span
					class="card-badge {automation.model_error ? 'card-badge--warning' : 'card-badge--muted'}"
					data-testid="automation-model-badge"
					title={automation.model_error || 'Model its runs use'}
				>{automationModelLabel(automation)}</span>
			</div>
			{#if automation.model_error}
				<p class="auto-detail-desc auto-model-error" role="alert">{automation.model_error}</p>
			{/if}
			{#if automation.description}
				<p class="auto-detail-desc">{automation.description}</p>
			{/if}
		</div>
		<div class="auto-detail-header-actions">
			<button class="card-action-btn" title="Edit" onclick={() => onEdit(automation)}>
				<Pencil size={16} />
			</button>
			<button class="card-action-btn" title="Trigger" onclick={() => onTrigger(automation.id)}>
				<Play size={16} />
			</button>
			<button class="card-action-btn card-action-btn--destructive" title="Delete" onclick={() => onDelete(automation.id)}>
				<Trash2 size={16} />
			</button>
		</div>
	</div>

	<!-- Stats summary -->
	{#if detailStats}
		<div class="auto-stats-row" transition:slide={{ duration: 150 }}>
			<div class="auto-stat">
				<span class="auto-stat-value">{detailStats.total_runs}</span>
				<span class="auto-stat-label">Total Runs</span>
			</div>
			<div class="auto-stat">
				<span class="auto-stat-value">{formatCost(detailStats.total_cost_usd)}</span>
				<span class="auto-stat-label">Total Cost</span>
			</div>
			<div class="auto-stat">
				<span class="auto-stat-value">{successRate(detailStats.total_runs, detailStats.successful_runs)}</span>
				<span class="auto-stat-label">Success Rate</span>
			</div>
			<div class="auto-stat">
				<span class="auto-stat-value">{detailStats.failed_runs}</span>
				<span class="auto-stat-label">Failures</span>
			</div>
		</div>
	{/if}

	<!-- Action Steps -->
	{#if automation.action_steps?.length}
		<div class="card-form-section" style="padding: 12px 16px; border-bottom: 1px solid var(--border-subtle, var(--border));">
			<span class="card-form-label">Action Steps</span>
			{#each automation.action_steps as step, i}
				<div class="auto-detail-row">
					<span class="auto-detail-label">Step {i + 1}</span>
					<span class="auto-detail-value">{step.action_id} on {step.connector_id}</span>
				</div>
			{/each}
		</div>
	{/if}

	<!-- Runs table -->
	<AutomationRunHistory
		{runs}
		{runsLoading}
		{expandedRunIds}
		{onToggleRunExpanded}
		{formatTime}
		{formatCost}
		{formatDuration}
		{getRunStatusBadgeClass}
	/>
</div>

<style>
	.auto-detail {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.auto-detail-header {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		flex-shrink: 0;
	}

	.auto-detail-header-info {
		flex: 1;
		min-width: 0;
	}

	.auto-detail-name-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.auto-detail-name {
		font-size: var(--text-lg, 1rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
		margin: 0;
	}

	.auto-detail-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, var(--muted-foreground));
		margin: 4px 0 0 0;
	}

	.auto-detail-desc.auto-model-error {
		color: var(--destructive);
	}

	.auto-detail-header-actions {
		display: flex;
		gap: 2px;
		flex-shrink: 0;
	}

	/* Stats row */
	.auto-stats-row {
		display: flex;
		gap: 16px;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		background: var(--surface-1);
		flex-shrink: 0;
		flex-wrap: wrap;
	}

	.auto-stat {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		flex: 1;
		min-width: 60px;
	}

	.auto-stat-value {
		font-size: var(--text-md, 0.875rem);
		font-weight: 700;
		color: var(--text-primary, var(--foreground));
	}

	.auto-stat-label {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, var(--muted-foreground));
	}

	/* Detail rows */
	.auto-detail-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 0;
		font-size: var(--text-sm, 0.75rem);
	}

	.auto-detail-label {
		color: var(--text-secondary, var(--muted-foreground));
		font-weight: 500;
		min-width: 60px;
	}

	.auto-detail-value {
		color: var(--text-primary, var(--foreground));
		font-family: var(--card-font-mono, monospace);
		font-size: var(--text-xs, 0.6875rem);
	}

	@media (max-width: 639px) {
		.auto-stats-row {
			gap: 8px;
		}
	}
</style>
