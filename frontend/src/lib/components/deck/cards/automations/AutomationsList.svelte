<script lang="ts">
	import { automationModelLabel, type Automation } from '$lib/api/automations';
	import { ListItem, EmptyDetail } from '$lib/components/layout/view';
 import type { Profile } from '$lib/api/client';
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
 selectedId?: string | null;
 profiles?: Profile[];
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
 selectedId = null, profiles = [],		automations,
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
 <EmptyDetail title={searchQuery ? 'No matching automations' : 'No automations yet'}
  message={searchQuery ? 'Try a different search term.' : 'Create a scheduled or event-driven task.'}>
  {#if !searchQuery}<button class="st-btn primary" onclick={onOpenCreate}>New automation</button>{/if}
 </EmptyDetail>
{:else}
 {#each filteredAutomations() as automation (automation.id)}
  <ListItem id={automation.id} name={automation.name} icon={CalendarClock}
   selected={selectedId === automation.id} onselect={() => onOpenDetail(automation)}
   status={!automation.is_enabled ? 'off' : automation.status === 'error' ? 'err' : 'ok'}
   summary={automation.description || (automation.triggers || [{type: automation.trigger_type}]).map(t => triggerTypeLabel(t.type)).join(' · ')}
   meta={automation.is_enabled ? automation.status === 'running' ? 'Running' : 'Enabled' : 'Disabled'}>
   {#snippet badges()}{#if !automation.profile_id}<span>No agent</span>{/if}{/snippet}
  </ListItem>
  {#if automation.last_error}<p class="px-3 pb-2 text-xs text-muted-foreground" role="status">{automation.last_error}</p>{/if}
 {/each}
{/if}
