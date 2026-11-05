<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/** Project-local agents in Settings rows. */
	import {
		projectAgents,
		allProjectAgents,
		projectAgentsLoading,
		projectAgentsError,
		projectAgentCount
	} from '$lib/stores/projectAgents.svelte';
	import { Search, Bot, Trash2 } from 'lucide-svelte';
	import { getModelLabel } from '$lib/constants/models';
	import { RowGroup, SettingRow, PanelSearch, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import './project-tabs.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Search state
	let searchQuery = $state('');

	// Delete confirmation
	let deletingAgent = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Touch/narrow only: which row has its collapsed ⋮ tray expanded (one at a
	// time; desktop keeps the always-inline hover rail). Outside click / Escape
	// collapses it and disarms any pending delete confirmation.
	let actionsOpenName = $state<string | null>(null);
	function toggleRowActions(e: MouseEvent | KeyboardEvent, name: string) {
		e.stopPropagation();
		actionsOpenName = actionsOpenName === name ? null : name;
	}
	function onWindowClick() {
		if (actionsOpenName !== null) actionsOpenName = null;
		if (deletingAgent !== null) deletingAgent = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			actionsOpenName = null;
			deletingAgent = null;
		}
	}

	// Load agents when project changes
	$effect(() => {
		if (projectId) {
			// A failed load is shown through $projectAgentsError.
			projectAgents.loadAgents(projectId).catch(() => {});
		}
	});

	// Filter agents
	const filteredAgents = $derived(() => {
		if (!searchQuery.trim()) return $allProjectAgents;
		const q = searchQuery.toLowerCase();
		return $allProjectAgents.filter(
			(a) =>
				a.name.toLowerCase().includes(q) ||
				a.description.toLowerCase().includes(q)
		);
	});

	// Project agents display "Inherit" when no model is set; otherwise the
	// shared registry handles label lookup.
	function getModelDisplay(model?: string | null): string {
		if (!model) return 'Inherit';
		return getModelLabel(model);
	}

	// Delete agent
	async function handleDelete(agentName: string) {
		deleteLoading = true;
		try {
			await projectAgents.deleteAgent(projectId, agentName);
			deletingAgent = null;
		} catch (e) {
			console.error('Failed to delete agent:', e);
		} finally {
			deleteLoading = false;
		}
	}
	watchResources(["agents", "projects"], () => projectId ? projectAgents.loadAgents(projectId) : undefined);
</script>
<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />
{#if $projectAgentsLoading}
	<p class="project-muted" role="status">Loading agents…</p>
{:else}
	{#if $projectAgentCount > 3}<PanelSearch bind:value={searchQuery} label="Search agents" placeholder="Search agents…" />{/if}
	{#if $projectAgentsError}<InlineBanner>{$projectAgentsError}{#snippet actions()}<button class="st-btn sm" onclick={() => projectAgents.clearError()}>Dismiss</button>{/snippet}</InlineBanner>{/if}
	{#if filteredAgents().length === 0}
		<EmptyDetail title={searchQuery ? `No agents match "${searchQuery}"` : 'No agents defined'} message="Create agents from the Subagents card to add specialized capabilities.">
			{#if searchQuery}<button class="st-btn" onclick={() => (searchQuery = '')}>Clear search</button>{/if}
		</EmptyDetail>
	{:else}
		<RowGroup title="Agents" count={filteredAgents().length}>
			{#each filteredAgents() as agent (agent.name)}
				<SettingRow label={deletingAgent === agent.name ? `Delete “${agent.name}”?` : agent.name} hint={deletingAgent === agent.name ? 'This removes the agent file. This cannot be undone.' : `${getModelDisplay(agent.model)} · ${agent.tools?.length ? `${agent.tools.length} tools` : 'All tools'}`}>
					{#if deletingAgent === agent.name}
						<button class="st-btn" onclick={(e) => { e.stopPropagation(); deletingAgent = null; }} disabled={deleteLoading}>Cancel</button>
						<button class="st-btn danger" onclick={(e) => { e.stopPropagation(); handleDelete(agent.name); }} disabled={deleteLoading}>{deleteLoading ? 'Deleting…' : 'Delete'}</button>
					{:else}
						<RowActionMenu open={actionsOpenName === agent.name} onToggle={(e) => toggleRowActions(e, agent.name)} label="Agent actions">
							<button class="st-btn icon danger" title="Delete agent" aria-label="Delete agent" onclick={(e) => { e.stopPropagation(); actionsOpenName = null; deletingAgent = agent.name; }}><Trash2 /></button>
						</RowActionMenu>
					{/if}
				</SettingRow>
			{/each}
		</RowGroup>
	{/if}
{/if}
