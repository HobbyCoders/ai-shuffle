<script lang="ts">
	/**
	 * AgentsView - Main agents management view
	 *
	 * Displays agents organized by status with tabs for:
	 * - Running agents
	 * - Queued agents
	 * - Completed agents
	 * - Failed agents
	 * - Statistics
	 */
	import { Rocket, Clock, CheckCircle, XCircle, BarChart3, Bot } from 'lucide-svelte';
	import AgentListItem from './AgentListItem.svelte';
	import AgentLauncher from './AgentLauncher.svelte';
	import AgentDetails from './AgentDetails.svelte';
	import AgentStats from './AgentStats.svelte';

	// Types
	type AgentStatus = 'running' | 'queued' | 'completed' | 'failed' | 'paused';

	interface Agent {
		id: string;
		name: string;
		status: AgentStatus;
		progress?: number;
		startedAt?: Date;
		completedAt?: Date;
		duration?: number;
		branch?: string;
		task?: string;
	}

	// State
	let activeTab = $state<'running' | 'queued' | 'completed' | 'failed' | 'stats'>('running');
	let showLauncher = $state(false);
	let selectedAgentId = $state<string | null>(null);

	// Mock data for display
	const mockAgents: Agent[] = [
		{
			id: '1',
			name: 'Implement user authentication',
			status: 'running',
			progress: 65,
			startedAt: new Date(Date.now() - 1800000),
			branch: 'feature/auth',
			task: 'Setting up JWT middleware'
		},
		{
			id: '2',
			name: 'Add dark mode support',
			status: 'running',
			progress: 30,
			startedAt: new Date(Date.now() - 600000),
			branch: 'feature/dark-mode',
			task: 'Creating theme context'
		},
		{
			id: '3',
			name: 'Fix navigation bug',
			status: 'queued',
			task: 'Waiting for agent slot'
		},
		{
			id: '4',
			name: 'Refactor API endpoints',
			status: 'completed',
			startedAt: new Date(Date.now() - 7200000),
			completedAt: new Date(Date.now() - 3600000),
			duration: 3600000,
			branch: 'feature/api-refactor'
		},
		{
			id: '5',
			name: 'Update database schema',
			status: 'completed',
			startedAt: new Date(Date.now() - 86400000),
			completedAt: new Date(Date.now() - 82800000),
			duration: 3600000,
			branch: 'feature/db-update'
		},
		{
			id: '6',
			name: 'Add email notifications',
			status: 'failed',
			startedAt: new Date(Date.now() - 3600000),
			completedAt: new Date(Date.now() - 3000000),
			duration: 600000,
			task: 'SMTP configuration error'
		}
	];

	// Filtered agents by status
	const runningAgents = $derived(mockAgents.filter(a => a.status === 'running' || a.status === 'paused'));
	const queuedAgents = $derived(mockAgents.filter(a => a.status === 'queued'));
	const completedAgents = $derived(mockAgents.filter(a => a.status === 'completed'));
	const failedAgents = $derived(mockAgents.filter(a => a.status === 'failed'));

	const selectedAgent = $derived(mockAgents.find(a => a.id === selectedAgentId));

	// Tab configuration - use function for counts to get reactive values
	function getTabCount(tabId: string): number | null {
		switch (tabId) {
			case 'running': return runningAgents.length;
			case 'queued': return queuedAgents.length;
			case 'completed': return completedAgents.length;
			case 'failed': return failedAgents.length;
			default: return null;
		}
	}

	const tabConfig = [
		{ id: 'running' as const, label: 'Running', icon: Rocket },
		{ id: 'queued' as const, label: 'Queued', icon: Clock },
		{ id: 'completed' as const, label: 'Completed', icon: CheckCircle },
		{ id: 'failed' as const, label: 'Failed', icon: XCircle },
		{ id: 'stats' as const, label: 'Stats', icon: BarChart3 }
	];

	function getAgentsForTab() {
		switch (activeTab) {
			case 'running': return runningAgents;
			case 'queued': return queuedAgents;
			case 'completed': return completedAgents;
			case 'failed': return failedAgents;
			default: return [];
		}
	}

	function handleAgentSelect(agentId: string) {
		selectedAgentId = agentId;
	}

	function handleLaunch() {
		showLauncher = true;
	}

	function handleLauncherClose() {
		showLauncher = false;
	}

	function handleLauncherSubmit(data: { name: string; prompt: string }) {
		console.log('Launch agent:', data);
		showLauncher = false;
	}

	function handleDetailsClose() {
		selectedAgentId = null;
	}

	// Empty state messages
	const emptyMessages = {
		running: {
			icon: Rocket,
			title: 'No agents running',
			description: 'Launch an agent to start automating tasks'
		},
		queued: {
			icon: Clock,
			title: 'No agents queued',
			description: 'All agents are running or completed'
		},
		completed: {
			icon: CheckCircle,
			title: 'No completed agents',
			description: 'Completed agents will appear here'
		},
		failed: {
			icon: XCircle,
			title: 'No failed agents',
			description: 'Failed agents will appear here for review'
		}
	};
</script>

<div class="h-full flex flex-col bg-background">
	<!-- Header -->
	<div class="flex items-center justify-between px-4 py-3 border-b border-border">
		<div class="flex items-center gap-2">
			<Bot class="w-5 h-5 text-primary" />
			<h2 class="text-lg font-semibold text-foreground">Agents</h2>
		</div>
		<button
			onclick={handleLaunch}
			class="flex items-center gap-2 px-3 py-1.5 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors text-sm font-medium"
		>
			<Rocket class="w-4 h-4" />
			Launch Agent
		</button>
	</div>

	<!-- Tabs -->
	<div class="flex items-center gap-1 px-4 py-2 border-b border-border overflow-x-auto">
		{#each tabConfig as tab}
			{@const Icon = tab.icon}
			{@const count = getTabCount(tab.id)}
			<button
				onclick={() => activeTab = tab.id}
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap {activeTab === tab.id ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			>
				<Icon class="w-4 h-4" />
				{tab.label}
				{#if count !== null}
					<span class="ml-1 px-1.5 py-0.5 text-xs rounded-full {activeTab === tab.id ? 'bg-primary/20' : 'bg-muted'}">
						{count}
					</span>
				{/if}
			</button>
		{/each}
	</div>

	<!-- Content -->
	<div class="flex-1 overflow-y-auto">
		{#if activeTab === 'stats'}
			<AgentStats />
		{:else}
			{@const agents = getAgentsForTab()}
			{@const empty = emptyMessages[activeTab]}
			{@const EmptyIcon = empty.icon}

			{#if agents.length === 0}
				<!-- Empty state -->
				<div class="flex flex-col items-center justify-center h-full p-8 text-center">
					<div class="w-16 h-16 rounded-full bg-muted flex items-center justify-center mb-4">
						<EmptyIcon class="w-8 h-8 text-muted-foreground" />
					</div>
					<h3 class="text-lg font-medium text-foreground mb-2">{empty.title}</h3>
					<p class="text-sm text-muted-foreground max-w-xs">{empty.description}</p>
					{#if activeTab === 'running'}
						<button
							onclick={handleLaunch}
							class="mt-4 flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors text-sm font-medium"
						>
							<Rocket class="w-4 h-4" />
							Launch Agent
						</button>
					{/if}
				</div>
			{:else}
				<!-- Agent list -->
				<div class="p-4 space-y-2">
					{#each agents as agent (agent.id)}
						<AgentListItem
							{agent}
							onSelect={() => handleAgentSelect(agent.id)}
						/>
					{/each}
				</div>
			{/if}
		{/if}
	</div>
</div>

<!-- Agent Launcher Modal -->
{#if showLauncher}
	<AgentLauncher
		onClose={handleLauncherClose}
		onLaunch={handleLauncherSubmit}
	/>
{/if}

<!-- Agent Details Panel -->
{#if selectedAgent}
	<AgentDetails
		agent={selectedAgent}
		onClose={handleDetailsClose}
	/>
{/if}
