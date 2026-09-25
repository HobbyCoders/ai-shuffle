<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * ProjectAgentsTab - Agent list tab for project detail view
	 *
	 * Studio-v2 grouped-field design (.set-group / .set-row), mirroring the
	 * Profile editor. Field primitives come from project-tabs.css (.ptab).
	 *
	 * - View agents in .fluid/agents/
	 * - Delete agents (inline confirm)
	 * - Search/filter
	 */
	import {
		projectAgents,
		allProjectAgents,
		projectAgentsLoading,
		projectAgentsError,
		projectAgentCount
	} from '$lib/stores/projectAgents.svelte';
	import { Search, Bot, Trash2 } from 'lucide-svelte';
	import { getModelLabel } from '$lib/constants/models';
	import Banner from '$lib/components/Banner.svelte';
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
			projectAgents.loadAgents(projectId);
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

<div class="ptab">
	{#if $projectAgentsLoading}
		<div class="ptab-loading"><div class="ptab-spinner"></div></div>
	{:else}
		<!-- Search (show if > 3 agents) -->
		{#if $projectAgentCount > 3}
			<div class="tsearch">
				<Search />
				<input type="text" bind:value={searchQuery} placeholder="Search agents…" class="inp" />
			</div>
		{/if}

		{#if $projectAgentsError}
			<Banner severity="error" message={$projectAgentsError} dismissible onDismiss={() => projectAgents.clearError()} />
		{/if}

		{#if filteredAgents().length === 0}
			<div class="empty-state">
				{#if searchQuery}
					<Search />
					<p class="es-title">No agents match "{searchQuery}"</p>
					<button class="ed-btn sm es-action" onclick={() => (searchQuery = '')}>Clear search</button>
				{:else}
					<Bot />
					<p class="es-title">No agents defined</p>
					<p class="es-desc">Create agents from the Subagents card to add specialized capabilities.</p>
				{/if}
			</div>
		{:else}
			<div class="set-group">
				<div class="set-grouphead">
					<h3><Bot />Agents</h3>
					<p>Custom sub-agents available to this project, from <code>.fluid/agents/</code>.</p>
				</div>
				{#each filteredAgents() as agent (agent.name)}
					{#if deletingAgent === agent.name}
						<div class="set-row stack">
							<div class="lab">
								<h4 class="danger">Delete “{agent.name}”?</h4>
								<p>This removes the agent file. This cannot be undone.</p>
							</div>
							<div class="ctl">
								<button class="ed-btn sm" onclick={(e) => { e.stopPropagation(); deletingAgent = null; }} disabled={deleteLoading}>Cancel</button>
								<button class="ed-btn sm danger" onclick={(e) => { e.stopPropagation(); handleDelete(agent.name); }} disabled={deleteLoading}>
									{deleteLoading ? 'Deleting…' : 'Delete'}
								</button>
							</div>
						</div>
					{:else}
						<div class="set-row">
							<div class="lab">
								<h4>{agent.name} <span class="badge muted">{getModelDisplay(agent.model)}</span></h4>
								{#if agent.tools && agent.tools.length > 0}
									<p>{agent.tools.length} tool{agent.tools.length === 1 ? '' : 's'}</p>
								{:else}
									<p>All tools</p>
								{/if}
							</div>
							<div class="ctl">
								<RowActionMenu
									open={actionsOpenName === agent.name}
									onToggle={(e) => toggleRowActions(e, agent.name)}
									label="Agent actions"
								>
									<button class="icon-btn danger" title="Delete agent" onclick={(e) => { e.stopPropagation(); actionsOpenName = null; deletingAgent = agent.name; }}>
										<Trash2 size={15} />
									</button>
								</RowActionMenu>
							</div>
						</div>
					{/if}
				{/each}
			</div>
		{/if}
	{/if}
</div>
