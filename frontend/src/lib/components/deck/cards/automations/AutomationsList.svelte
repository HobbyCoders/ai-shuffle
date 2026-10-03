<script lang="ts">
	import { automationModelLabel, type Automation } from '$lib/api/automations';
	import { slide } from 'svelte/transition';
	import {
		CalendarClock,
		Clock,
		ChevronRight,
		ToggleLeft,
		ToggleRight,
		Play,
		Pencil,
		Trash2,
		Loader2
	} from 'lucide-svelte';

	interface Props {
		automations: Automation[];
		searchQuery: string;
		triggeringId: string | null;
		onOpenDetail: (automation: Automation) => void;
		onOpenCreate: () => void;
		onOpenEdit: (automation: Automation) => void;
		onToggleEnabled: (automation: Automation) => void;
		onTrigger: (id: string) => void;
		onDelete: (id: string) => void;
		formatTime: (dateStr: string | null) => string;
		formatCost: (cost: number) => string;
		triggerTypeLabel: (type: any) => string;
		getStatusColor: (status: Automation['status']) => string;
	}

	let {
		automations,
		searchQuery,
		triggeringId,
		onOpenDetail,
		onOpenCreate,
		onOpenEdit,
		onToggleEnabled,
		onTrigger,
		onDelete,
		formatTime,
		formatCost,
		triggerTypeLabel,
		getStatusColor
	}: Props = $props();

	const filteredAutomations = $derived(() => {
		if (!searchQuery.trim()) return automations;
		const q = searchQuery.toLowerCase();
		return automations.filter(
			(a) =>
				a.name.toLowerCase().includes(q) ||
				a.trigger_type.toLowerCase().includes(q) ||
				(a.triggers || []).some(t => t.type.toLowerCase().includes(q)) ||
				a.description?.toLowerCase().includes(q)
		);
	});
</script>

{#if filteredAutomations().length === 0}
	<div class="card-empty-state">
		<div class="card-empty-icon">
			<CalendarClock size={32} />
		</div>
		<p class="card-empty-title">
			{searchQuery ? 'No matching automations' : 'No automations yet'}
		</p>
		<p class="card-empty-description">
			{searchQuery ? 'Try a different search term' : 'Create automations to run AI tasks on a schedule or in response to events'}
		</p>
		{#if !searchQuery}
			<button class="card-empty-action" onclick={onOpenCreate}>
				Create your first automation
			</button>
		{/if}
	</div>
{:else}
	<div class="fp-list">
		{#each filteredAutomations() as automation, idx (automation.id)}
			{#if idx > 0}
				<div class="fp-list-divider"></div>
			{/if}
			<div class="fp-list-item" transition:slide={{ duration: 150 }}>
				<div class="fp-item-indicator" style="background: {getStatusColor(automation.status)}" title={automation.status}></div>

				<button class="fp-item-content" style="background:none;border:none;cursor:pointer;text-align:left;padding:0;color:inherit;" onclick={() => onOpenDetail(automation)}>
					<div class="fp-item-header">
						<span class="fp-item-name">{automation.name}</span>
						{#each (automation.triggers || [{ type: automation.trigger_type }]).slice(0, 3) as trig}
							<span class="fp-item-badge">{triggerTypeLabel(trig.type)}</span>
						{/each}
						{#if (automation.triggers || []).length > 3}
							<span class="fp-item-badge">+{(automation.triggers || []).length - 3}</span>
						{/if}
						{#if !automation.is_enabled}
							<span class="card-badge card-badge--warning">Disabled</span>
						{/if}
					</div>
					{#if automation.description}
						<span class="fp-item-description">{automation.description}</span>
					{/if}
					<div class="fp-item-meta">
						<span class="fp-meta-chip">
							<Clock size={11} />
							{formatTime(automation.last_run_at)}
						</span>
						<span class="fp-meta-sep">|</span>
						<span class="fp-meta-chip">{formatCost(automation.total_cost_used)}</span>
						<span class="fp-meta-sep">|</span>
						<span class="fp-meta-chip">{automation.run_count} runs</span>
						<span class="fp-meta-sep">|</span>
						<span class="fp-meta-chip" title={automation.model_error || 'Model its runs use'}>{automationModelLabel(automation)}</span>
					</div>
				</button>

				<!-- Actions (hidden until hover via fp-item-actions) -->
				<div class="fp-item-actions">
					<button
						class="card-action-btn"
						title={automation.is_enabled ? 'Disable' : 'Enable'}
						onclick={() => onToggleEnabled(automation)}
					>
						{#if automation.is_enabled}
							<ToggleRight size={16} />
						{:else}
							<ToggleLeft size={16} />
						{/if}
					</button>
					<button
						class="card-action-btn"
						title="Trigger now"
						disabled={triggeringId === automation.id}
						onclick={() => onTrigger(automation.id)}
					>
						{#if triggeringId === automation.id}
							<Loader2 size={16} class="animate-spin" />
						{:else}
							<Play size={16} />
						{/if}
					</button>
					<button
						class="card-action-btn"
						title="Edit"
						onclick={() => onOpenEdit(automation)}
					>
						<Pencil size={16} />
					</button>
					<button
						class="card-action-btn card-action-btn--destructive"
						title="Delete"
						onclick={() => onDelete(automation.id)}
					>
						<Trash2 size={16} />
					</button>
				</div>

				<div class="fp-item-chevron">
					<ChevronRight size={16} />
				</div>
			</div>
		{/each}
	</div>
{/if}
