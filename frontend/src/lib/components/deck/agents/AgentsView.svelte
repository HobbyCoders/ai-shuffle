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
	import { onMount } from 'svelte';
	import { Rocket, Clock, CheckCircle, XCircle, BarChart3, Bot, RefreshCw, Loader2, AlertCircle } from 'lucide-svelte';
	import AgentListItem from './AgentListItem.svelte';
	import AgentStats from './AgentStats.svelte';
	import {
		agents,
		runningAgents,
		queuedAgents,
		completedAgents,
		failedAgents,
		pausedAgents,
		agentsLoading,
		agentsError,
		type BackgroundAgent
	} from '$lib/stores/agents';
	import { deck } from '$lib/stores/deck';

	// State
	let activeTab = $state<'running' | 'queued' | 'completed' | 'failed' | 'stats'>('running');
	let isRefreshing = $state(false);

	// Derived state from stores
	const running = $derived($runningAgents);
	const queued = $derived($queuedAgents);
	const completed = $derived($completedAgents);
	const failed = $derived($failedAgents);
	const paused = $derived($pausedAgents);
	const loading = $derived($agentsLoading);
	const error = $derived($agentsError);

	// Combined running + paused for active tab
	const activeAgentsForTab = $derived([...running, ...paused]);

	// Initialize store on mount
	// Note: WebSocket connection is intentionally NOT disconnected on component destroy
	// because agents run in the background and should maintain their connection
	// across navigation/component lifecycle changes
	onMount(() => {
		agents.init();
	});

	// Tab configuration - use function for counts to get reactive values
	function getTabCount(tabId: string): number | null {
		switch (tabId) {
			case 'running': return activeAgentsForTab.length;
			case 'queued': return queued.length;
			case 'completed': return completed.length;
			case 'failed': return failed.length;
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

	function getAgentsForTab(): BackgroundAgent[] {
		switch (activeTab) {
			case 'running': return activeAgentsForTab;
			case 'queued': return queued;
			case 'completed': return completed;
			case 'failed': return failed;
			default: return [];
		}
	}

	function handleAgentSelect(agentId: string) {
		// Find the agent to get its name for the card title
		const all = [...running, ...queued, ...completed, ...failed, ...paused];
		const agent = all.find(a => a.id === agentId);
		const agentName = agent?.name ?? 'Agent Monitor';

		// Switch to workspace mode and open the agent card
		deck.setMode('workspace');
		deck.openOrFocusCard('agent-monitor', agentId, {
			title: agentName,
			meta: { agentId }
		});
	}

	function handleLaunch() {
		// Open an agent card in launcher mode (no agentId)
		// The card will show the launcher UI, and upon launching, transition to monitor mode
		deck.setMode('workspace');
		deck.addCard('agent-monitor', {
			title: 'Launch Agent'
			// Note: no dataId or agentId meta = launcher mode
		});
	}


	async function handleRefresh() {
		isRefreshing = true;
		try {
			await agents.refresh();
		} finally {
			isRefreshing = false;
		}
	}

	async function handlePause(agentId: string) {
		try {
			await agents.pauseAgent(agentId);
		} catch (err) {
			console.error('Failed to pause agent:', err);
		}
	}

	async function handleResume(agentId: string) {
		try {
			await agents.resumeAgent(agentId);
		} catch (err) {
			console.error('Failed to resume agent:', err);
		}
	}

	async function handleCancel(agentId: string) {
		try {
			await agents.cancelAgent(agentId);
		} catch (err) {
			console.error('Failed to cancel agent:', err);
		}
	}

	async function handleDelete(agentId: string) {
		try {
			await agents.deleteAgent(agentId);
		} catch (err) {
			console.error('Failed to delete agent:', err);
		}
	}

	async function handleClearCompleted() {
		try {
			await agents.clearCompleted();
		} catch (err) {
			console.error('Failed to clear completed agents:', err);
		}
	}

	async function handleClearFailed() {
		try {
			await agents.clearFailed();
		} catch (err) {
			console.error('Failed to clear failed agents:', err);
		}
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
		<div class="flex items-center gap-2">
			<button
				onclick={handleRefresh}
				disabled={isRefreshing || loading}
				class="p-2 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors disabled:opacity-50"
				title="Refresh agents"
			>
				<RefreshCw class="w-4 h-4 {isRefreshing ? 'animate-spin' : ''}" />
			</button>
			<button
				onclick={handleLaunch}
				class="flex items-center gap-2 px-3 py-1.5 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors text-sm font-medium"
			>
				<Rocket class="w-4 h-4" />
				Launch Agent
			</button>
		</div>
	</div>

	<!-- Error banner -->
	{#if error}
		<div class="px-4 py-2 bg-red-500/10 border-b border-red-500/20 flex items-center gap-2">
			<AlertCircle class="w-4 h-4 text-red-500" />
			<span class="text-sm text-red-500">{error}</span>
			<button
				onclick={handleRefresh}
				class="ml-auto text-xs text-red-500 hover:underline"
			>
				Retry
			</button>
		</div>
	{/if}

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

		<!-- Clear buttons for completed/failed tabs -->
		{#if activeTab === 'completed' && completed.length > 0}
			<button
				onclick={handleClearCompleted}
				class="ml-auto text-xs text-muted-foreground hover:text-foreground"
			>
				Clear all
			</button>
		{/if}
		{#if activeTab === 'failed' && failed.length > 0}
			<button
				onclick={handleClearFailed}
				class="ml-auto text-xs text-muted-foreground hover:text-foreground"
			>
				Clear all
			</button>
		{/if}
	</div>

	<!-- Content -->
	<div class="flex-1 overflow-y-auto">
		{#if loading && !isRefreshing}
			<!-- Loading state -->
			<div class="flex flex-col items-center justify-center h-full p-8">
				<Loader2 class="w-8 h-8 text-primary animate-spin mb-4" />
				<p class="text-sm text-muted-foreground">Loading agents...</p>
			</div>
		{:else if activeTab === 'stats'}
			<AgentStats />
		{:else}
			{@const agentsList = getAgentsForTab()}
			{@const empty = emptyMessages[activeTab]}
			{@const EmptyIcon = empty.icon}

			{#if agentsList.length === 0}
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
					{#each agentsList as agent (agent.id)}
						<AgentListItem
							{agent}
							onSelect={() => handleAgentSelect(agent.id)}
							onPause={() => handlePause(agent.id)}
							onResume={() => handleResume(agent.id)}
							onCancel={() => handleCancel(agent.id)}
						/>
					{/each}
				</div>
			{/if}
		{/if}
	</div>
</div>


