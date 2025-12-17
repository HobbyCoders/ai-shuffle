<script lang="ts">
	/**
	 * AgentsView - Main agents interface when in Agents mode
	 *
	 * Features:
	 * - Header with "Launch Agent" button
	 * - Tabs: Running, Queued, Completed, Failed
	 * - List/grid of agent cards
	 * - Empty states for each tab
	 * - Real-time updates from agents store
	 */
	import {
		agents,
		allAgents,
		runningAgents,
		queuedAgents,
		completedAgents,
		failedAgents,
		pausedAgents,
		type BackgroundAgent
	} from '$lib/stores/agents';
	import AgentListItem from './AgentListItem.svelte';
	import AgentDetails from './AgentDetails.svelte';
	import AgentLauncher from './AgentLauncher.svelte';
	import AgentStats from './AgentStats.svelte';

	interface Profile {
		id: string;
		name: string;
		description?: string;
	}

	interface Project {
		id: string;
		name: string;
		path: string;
	}

	interface Props {
		profiles?: Profile[];
		projects?: Project[];
	}

	let { profiles = [], projects = [] }: Props = $props();

	// Tab state
	type TabId = 'running' | 'queued' | 'completed' | 'failed' | 'stats';
	let activeTab = $state<TabId>('running');

	// Modal states
	let showLauncher = $state(false);
	let selectedAgentId = $state<string | null>(null);

	// Get selected agent
	const selectedAgent = $derived(
		selectedAgentId ? agents.getAgent(selectedAgentId) : null
	);

	// Tab definitions with counts
	const tabs = $derived([
		{ id: 'running' as const, label: 'Running', count: $runningAgents.length + $pausedAgents.length },
		{ id: 'queued' as const, label: 'Queued', count: $queuedAgents.length },
		{ id: 'completed' as const, label: 'Completed', count: $completedAgents.length },
		{ id: 'failed' as const, label: 'Failed', count: $failedAgents.length },
		{ id: 'stats' as const, label: 'Stats', count: 0 }
	]);

	// Get agents for current tab
	const currentAgents = $derived.by((): BackgroundAgent[] => {
		switch (activeTab) {
			case 'running':
				return [...$runningAgents, ...$pausedAgents].sort(
					(a, b) => b.startedAt.getTime() - a.startedAt.getTime()
				);
			case 'queued':
				return $queuedAgents.sort((a, b) => a.startedAt.getTime() - b.startedAt.getTime());
			case 'completed':
				return $completedAgents.sort(
					(a, b) => (b.completedAt?.getTime() || 0) - (a.completedAt?.getTime() || 0)
				);
			case 'failed':
				return $failedAgents.sort(
					(a, b) => (b.completedAt?.getTime() || 0) - (a.completedAt?.getTime() || 0)
				);
			default:
				return [];
		}
	});

	// Handle agent launch
	async function handleLaunch(config: {
		name: string;
		prompt: string;
		profileId?: string;
		projectId?: string;
		autoCreateBranch: boolean;
		branchName?: string;
	}) {
		try {
			await agents.launchAgent(config.name, config.prompt, {
				profileId: config.profileId,
				projectId: config.projectId,
				branch: config.branchName,
				autoCreateBranch: config.autoCreateBranch
			});
			showLauncher = false;
			activeTab = 'running';
		} catch (e) {
			console.error('Failed to launch agent:', e);
		}
	}

	// Agent actions
	function handlePause(agentId: string) {
		agents.pauseAgent(agentId);
	}

	function handleResume(agentId: string) {
		agents.resumeAgent(agentId);
	}

	function handleCancel(agentId: string) {
		agents.cancelAgent(agentId);
	}

	function handleIntervene(agentId: string) {
		agents.interveneAgent(agentId);
		selectedAgentId = null;
	}

	function handleRestart(agent: BackgroundAgent) {
		// Re-launch with same config
		agents.launchAgent(agent.name, agent.prompt, {
			profileId: agent.profileId,
			projectId: agent.projectId,
			branch: agent.branch,
			autoCreateBranch: !!agent.branch
		});
		selectedAgentId = null;
	}

	function clearCompleted() {
		agents.clearCompletedAgents();
	}

	// Get empty state info
	function getEmptyState(tab: TabId): { icon: string; title: string; description: string } {
		switch (tab) {
			case 'running':
				return {
					icon: 'M13 10V3L4 14h7v7l9-11h-7z',
					title: 'No running agents',
					description: 'Launch a new agent to start autonomous work'
				};
			case 'queued':
				return {
					icon: 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z',
					title: 'Queue is empty',
					description: 'Agents waiting to start will appear here'
				};
			case 'completed':
				return {
					icon: 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z',
					title: 'No completed agents',
					description: 'Successfully completed agents will appear here'
				};
			case 'failed':
				return {
					icon: 'M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z',
					title: 'No failed agents',
					description: 'Agents that encounter errors will appear here'
				};
			default:
				return {
					icon: 'M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z',
					title: 'No agents',
					description: 'Launch an agent to get started'
				};
		}
	}

	const emptyState = $derived(getEmptyState(activeTab));
</script>

<div class="agents-view h-full flex flex-col bg-background">
	<!-- Header -->
	<header class="shrink-0 px-6 py-4 border-b border-border bg-gradient-to-r from-background to-muted/30">
		<div class="flex items-center justify-between gap-4">
			<div>
				<h1 class="text-xl font-semibold text-foreground flex items-center gap-3">
					<div class="w-8 h-8 rounded-lg bg-cyan-500/15 flex items-center justify-center">
						<svg class="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
						</svg>
					</div>
					Mission Control
				</h1>
				<p class="text-sm text-muted-foreground mt-1">
					Manage autonomous background agents
				</p>
			</div>

			<button
				type="button"
				onclick={() => showLauncher = true}
				class="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-500 text-white font-medium hover:from-cyan-400 hover:to-blue-400 transition-all shadow-lg shadow-cyan-500/20"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
				Launch Agent
			</button>
		</div>

		<!-- Tab navigation -->
		<nav class="flex gap-1 mt-4 -mb-4 overflow-x-auto scrollbar-none" aria-label="Agent tabs">
			{#each tabs as tab}
				<button
					type="button"
					onclick={() => activeTab = tab.id}
					class="flex items-center gap-2 px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all whitespace-nowrap border-b-2 -mb-px
						{activeTab === tab.id
							? 'text-primary border-primary bg-primary/5'
							: 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}"
					aria-current={activeTab === tab.id ? 'page' : undefined}
				>
					{tab.label}
					{#if tab.count > 0}
						<span class="px-1.5 py-0.5 text-[10px] rounded-full {activeTab === tab.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">
							{tab.count}
						</span>
					{/if}
				</button>
			{/each}
		</nav>
	</header>

	<!-- Content -->
	<div class="flex-1 overflow-y-auto">
		{#if activeTab === 'stats'}
			<!-- Stats view -->
			<AgentStats agents={$allAgents} />
		{:else if currentAgents.length === 0}
			<!-- Empty state -->
			<div class="flex flex-col items-center justify-center h-full py-16 px-6">
				<div class="w-20 h-20 rounded-2xl bg-muted/50 flex items-center justify-center mb-6">
					<svg class="w-10 h-10 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d={emptyState.icon} />
					</svg>
				</div>
				<h3 class="text-lg font-medium text-foreground mb-2">{emptyState.title}</h3>
				<p class="text-sm text-muted-foreground text-center max-w-sm">{emptyState.description}</p>

				{#if activeTab === 'running'}
					<button
						type="button"
						onclick={() => showLauncher = true}
						class="mt-6 flex items-center gap-2 px-4 py-2 rounded-lg border-2 border-dashed border-border text-muted-foreground hover:text-foreground hover:border-primary/50 transition-all"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
						</svg>
						Launch your first agent
					</button>
				{/if}
			</div>
		{:else}
			<!-- Agent list -->
			<div class="p-4 space-y-2">
				{#if activeTab === 'completed' || activeTab === 'failed'}
					<div class="flex justify-end mb-2">
						<button
							type="button"
							onclick={clearCompleted}
							class="text-xs text-muted-foreground hover:text-foreground transition-colors"
						>
							Clear all
						</button>
					</div>
				{/if}

				{#each currentAgents as agent (agent.id)}
					<AgentListItem
						{agent}
						selected={selectedAgentId === agent.id}
						onselect={() => selectedAgentId = agent.id}
						onpause={() => handlePause(agent.id)}
						onresume={() => handleResume(agent.id)}
						oncancel={() => handleCancel(agent.id)}
						onview={() => selectedAgentId = agent.id}
					/>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Active agents indicator bar -->
	{#if $runningAgents.length > 0}
		<div class="shrink-0 px-4 py-2 border-t border-border bg-cyan-500/5 flex items-center gap-3">
			<div class="flex items-center gap-2">
				<span class="w-2 h-2 rounded-full bg-cyan-500 animate-pulse"></span>
				<span class="text-xs text-cyan-400 font-medium">
					{$runningAgents.length} agent{$runningAgents.length !== 1 ? 's' : ''} working
				</span>
			</div>

			<!-- Mini progress indicators -->
			<div class="flex-1 flex items-center gap-2 overflow-x-auto scrollbar-none">
				{#each $runningAgents.slice(0, 5) as agent (agent.id)}
					<button
						type="button"
						onclick={() => selectedAgentId = agent.id}
						class="flex items-center gap-2 px-2 py-1 rounded-lg bg-card/50 hover:bg-card transition-colors text-xs"
					>
						<span class="text-foreground truncate max-w-[100px]">{agent.name}</span>
						<span class="text-muted-foreground">{agent.progress}%</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}
</div>

<!-- Agent Launcher Modal -->
<AgentLauncher
	open={showLauncher}
	{profiles}
	{projects}
	onlaunch={handleLaunch}
	oncancel={() => showLauncher = false}
/>

<!-- Agent Details Modal -->
{#if selectedAgent}
	<AgentDetails
		agent={selectedAgent}
		open={true}
		onclose={() => selectedAgentId = null}
		onpause={() => handlePause(selectedAgent.id)}
		onresume={() => handleResume(selectedAgent.id)}
		oncancel={() => handleCancel(selectedAgent.id)}
		onintervene={() => handleIntervene(selectedAgent.id)}
		onrestart={() => handleRestart(selectedAgent)}
	/>
{/if}

<style>
	.scrollbar-none {
		scrollbar-width: none;
		-ms-overflow-style: none;
	}

	.scrollbar-none::-webkit-scrollbar {
		display: none;
	}
</style>
