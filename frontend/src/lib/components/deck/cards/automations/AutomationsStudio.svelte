<script lang="ts">
	/**
	 * AutomationsStudio — the Studio-v2 Automations surface.
	 *
	 * Matches the mockup `#page-automations`: a Studio page header (eyebrow + serif
	 * title + count subline + "New automation"), an approval banner per pending run,
	 * and a `.lrow`-style run list (trigger icon + name/status + trigger/profile/cost
	 * meta). Reuses AutomationsCard's data + handlers; only the list view is new.
	 * Theme tokens only — never the mockup palette.
	 */
	import {
		automationModelLabel,
		type Automation,
		type AutomationApproval,
		type TriggerType
	} from '$lib/api/automations';
	import type { Profile } from '$lib/api/client';
	import { ViewPanel, PanelSearch, PanelIconButton, InlineBanner, RowGroup, SettingRow } from '$lib/components/layout/view';
 import AutomationsList from './AutomationsList.svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import {
		Search, Plus, Clock, Activity, Globe, Webhook, CalendarClock,
		Play, Pencil, Trash2, AlertCircle, AlertTriangle, X, Loader2
	} from 'lucide-svelte';

	interface Props {
 selectedId?: string | null;
		automations: Automation[];
		pendingApprovals: AutomationApproval[];
		profiles: Profile[];
		loading?: boolean;
		error?: string;
		triggeringId?: string | null;
		onOpenCreate: () => void;
		onOpenDetail: (automation: Automation) => void;
		onOpenEdit: (automation: Automation) => void;
		onToggleEnabled: (automation: Automation) => void;
		onTrigger: (id: string) => void;
		onDelete: (id: string) => void;
		onApprove: (approvalId: string) => void;
		onDeny: (approvalId: string) => void;
		onClearError: () => void;
		formatCost: (cost: number) => string;
		triggerTypeLabel: (type: TriggerType) => string;
	}

	let {
 selectedId = null,		automations,
		pendingApprovals,
		profiles,
		loading = false,
		error = '',
		triggeringId = null,
		onOpenCreate,
		onOpenDetail,
		onOpenEdit,
		onToggleEnabled,
		onTrigger,
		onDelete,
		onApprove,
		onDeny,
		onClearError,
		formatCost,
		triggerTypeLabel
	}: Props = $props();

	let searchQuery = $state('');
</script>
<ViewPanel title="Automations" count={automations.length} {loading}>
 {#snippet actions()}<PanelIconButton label="New automation" icon={Plus} onclick={onOpenCreate} />{/snippet}
 {#snippet search()}<PanelSearch bind:value={searchQuery} placeholder="Search automations" />{/snippet}
 {#if error}<InlineBanner tone="warn">{error}<button class="st-btn" onclick={onClearError}>Dismiss</button></InlineBanner>{/if}
 {#if loading}<p class="p-4 text-sm text-muted-foreground" role="status">Loading automations…</p>
 {:else}
  <AutomationsList {automations} {searchQuery} {triggeringId} {selectedId} {profiles}
   {onOpenDetail} {onOpenCreate} {onOpenEdit} {onToggleEnabled} {onTrigger} {onDelete}
   {formatCost} {triggerTypeLabel} formatTime={(value) => value ? new Date(value).toLocaleString() : 'Never'} getStatusColor={() => ''} />
 {/if}
 {#if pendingApprovals.length}
  <RowGroup title="Pending approvals" count={pendingApprovals.length} bare>
   {#each pendingApprovals as approval (approval.id)}
    <SettingRow label={approval.automation_name || 'Automation'} hint={approval.prompt_preview || 'Waiting for your approval.'}>
     <button class="st-btn primary" onclick={() => onApprove(approval.id)}>Approve</button>
     <button class="st-btn" onclick={() => onDeny(approval.id)}>Deny</button>
    </SettingRow>
   {/each}
  </RowGroup>
 {/if}
</ViewPanel>
