<script lang="ts">
	/**
	 * AgentCard - Agent monitoring card for The Deck
	 *
	 * Displays agent status, tasks, logs, and controls for monitoring
	 * background agents. Agent launching is handled separately.
	 */

	import { onMount, onDestroy, untrack } from 'svelte';
	import {
		Play,
		Pause,
		Square,
		GitBranch,
		ChevronDown,
		ChevronRight,
		CheckCircle,
		Circle,
		Loader2,
		AlertCircle,
		ExternalLink,
		Trash2
	} from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import {
		agents,
		allAgents,
		type BackgroundAgent,
		type AgentTask,
		type AgentLogEntry
	} from '$lib/stores/agents';

	interface Props {
		card: DeckCard;
		agentId: string;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		agentId,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// Debug: Log props on mount
	console.log('[AgentCard] Component created with agentId:', agentId, 'card:', card?.id, card?.dataId);

	// ============================================
	// MONITOR STATE - Reactive subscription to store
	// ============================================
	// Subscribe to the agents store reactively - this will update whenever the store changes
	const agent = $derived.by(() => {
		const agentsList = $allAgents;
		return agentsList.find(a => a.id === agentId) || null;
	});

	// Loading state: show loading only if agent not yet in store and we haven't finished initial fetch
	let initialFetchDone = $state(false);
	let fetchError = $state<string | null>(null);

	// Derived loading state - shows loading spinner only when needed
	const loading = $derived(!agent && !initialFetchDone);
	const error = $derived(fetchError);

	// Task tree expanded state
	let expandedTasks = $state<Set<string>>(new Set());

	// Duration timer
	let duration = $state('0:00');
	let durationInterval: ReturnType<typeof setInterval> | null = null;

	// Time remaining for timeout
	let timeRemaining = $state<string | null>(null);

	// Fetch agent data initially
	async function fetchAgent() {
		console.log('[AgentCard] fetchAgent called, agentId:', agentId);
		if (!agentId) {
			console.log('[AgentCard] No agentId, returning');
			return;
		}

		fetchError = null;
		try {
			console.log('[AgentCard] Calling agents.fetchAgent...');
			const fetchedAgent = await agents.fetchAgent(agentId);
			console.log('[AgentCard] fetchedAgent result:', fetchedAgent);
			if (fetchedAgent) {
				// Auto-expand tasks with running children
				const runningTaskIds = new Set<string>();
				function findRunningParents(tasks: AgentTask[], parentIds: string[] = []) {
					for (const task of tasks) {
						if (task.status === 'in_progress') {
							parentIds.forEach((id) => runningTaskIds.add(id));
						}
						if (task.children) {
							findRunningParents(task.children, [...parentIds, task.id]);
						}
					}
				}
				findRunningParents(fetchedAgent.tasks);
				expandedTasks = runningTaskIds;

				// Fetch logs
				await agents.fetchLogs(agentId, { limit: 10 });
			} else {
				fetchError = 'Agent not found';
			}
		} catch (e) {
			console.error('[AgentCard] Error fetching agent:', e);
			fetchError = e instanceof Error ? e.message : 'Failed to load agent';
		} finally {
			initialFetchDone = true;
			console.log('[AgentCard] fetchAgent complete, initialFetchDone=true, fetchError:', fetchError);
		}
	}

	// Initial fetch and WebSocket subscription
	$effect(() => {
		if (agentId && !initialFetchDone) {
			fetchAgent();
			agents.subscribeToAgent(agentId);
		}
	});

	// Auto-expand tasks when agent updates (for running tasks)
	$effect(() => {
		if (agent?.tasks) {
			const runningTaskIds = new Set<string>();
			function findRunningParents(tasks: AgentTask[], parentIds: string[] = []) {
				for (const task of tasks) {
					if (task.status === 'in_progress') {
						parentIds.forEach((id) => runningTaskIds.add(id));
					}
					if (task.children) {
						findRunningParents(task.children, [...parentIds, task.id]);
					}
				}
			}
			findRunningParents(agent.tasks);
			// Merge with existing expanded tasks (don't collapse user-expanded ones)
			// Use untrack to prevent reading expandedTasks from triggering the effect again
			const currentExpanded = untrack(() => expandedTasks);
			const merged = new Set([...currentExpanded, ...runningTaskIds]);
			// Only update if there are new items to add (prevents unnecessary re-renders)
			if (merged.size !== currentExpanded.size) {
				expandedTasks = merged;
			}
		}
	});

	// Periodic log refresh for running agents (logs don't always come through WebSocket)
	let logRefreshInterval: ReturnType<typeof setInterval> | null = null;

	$effect(() => {
		if (logRefreshInterval) {
			clearInterval(logRefreshInterval);
			logRefreshInterval = null;
		}

		if (agentId && agent?.status === 'running') {
			logRefreshInterval = setInterval(async () => {
				try {
					await agents.fetchLogs(agentId, { limit: 10 });
				} catch (e) {
					console.error('[AgentCard] Failed to refresh logs:', e);
				}
			}, 3000); // Refresh logs every 3 seconds while running
		}

		return () => {
			if (logRefreshInterval) {
				clearInterval(logRefreshInterval);
				logRefreshInterval = null;
			}
		};
	});

	onMount(() => {
		// Agent data is fetched via $effect
	});

	onDestroy(() => {
		if (agentId) {
			agents.unsubscribeFromAgent(agentId);
		}
		if (durationInterval) {
			clearInterval(durationInterval);
		}
		if (logRefreshInterval) {
			clearInterval(logRefreshInterval);
		}
	});

	// Duration timer effect
	$effect(() => {
		if (durationInterval) {
			clearInterval(durationInterval);
			durationInterval = null;
		}

		if (agent?.status !== 'running' || !agent?.startedAt) {
			return;
		}

		function updateDuration() {
			if (!agent?.startedAt) return;
			const elapsed = Math.floor((Date.now() - agent.startedAt.getTime()) / 1000);
			const mins = Math.floor(elapsed / 60);
			const secs = elapsed % 60;
			duration = `${mins}:${secs.toString().padStart(2, '0')}`;

			// Calculate time remaining (if maxDurationMinutes is set and > 0)
			const maxDurationMinutes = agent?.maxDurationMinutes;
			if (maxDurationMinutes && maxDurationMinutes > 0) {
				const maxDurationSeconds = maxDurationMinutes * 60;
				const remainingSeconds = maxDurationSeconds - elapsed;
				if (remainingSeconds > 0) {
					const remainingMins = Math.floor(remainingSeconds / 60);
					const remainingSecs = remainingSeconds % 60;
					timeRemaining = `${remainingMins}:${remainingSecs.toString().padStart(2, '0')}`;
				} else {
					timeRemaining = '0:00';
				}
			} else {
				// Unlimited duration
				timeRemaining = null;
			}
		}

		updateDuration();
		durationInterval = setInterval(updateDuration, 1000);

		return () => {
			if (durationInterval) {
				clearInterval(durationInterval);
				durationInterval = null;
			}
		};
	});

	// Status colors and icons - using semantic CSS variables
	const statusConfig = {
		running: { color: 'status-running', label: 'Running' },
		paused: { color: 'status-warning', label: 'Paused' },
		queued: { color: 'status-muted', label: 'Queued' },
		failed: { color: 'status-destructive', label: 'Failed' },
		completed: { color: 'status-success', label: 'Completed' }
	};

	const taskStatusIcons = {
		pending: Circle,
		in_progress: Loader2,
		completed: CheckCircle,
		failed: AlertCircle
	};

	// Toggle task expansion
	function toggleTask(taskId: string) {
		if (expandedTasks.has(taskId)) {
			expandedTasks.delete(taskId);
			expandedTasks = new Set(expandedTasks);
		} else {
			expandedTasks.add(taskId);
			expandedTasks = new Set(expandedTasks);
		}
	}

	// Control actions - store updates automatically via optimistic updates + WebSocket
	async function handlePause() {
		if (!agent || !agentId) return;
		try {
			await agents.pauseAgent(agentId);
			// Store updates automatically via optimistic update
		} catch (e) {
			console.error('Failed to pause agent:', e);
		}
	}

	async function handleResume() {
		if (!agent || !agentId) return;
		try {
			await agents.resumeAgent(agentId);
			// Store updates automatically via optimistic update
		} catch (e) {
			console.error('Failed to resume agent:', e);
		}
	}

	async function handleCancel() {
		if (!agent || !agentId) return;
		try {
			await agents.cancelAgent(agentId);
			// Store updates automatically via optimistic update
		} catch (e) {
			console.error('Failed to cancel agent:', e);
		}
	}

	async function handleDelete() {
		if (!agent || !agentId) return;
		try {
			await agents.deleteAgent(agentId);
			onClose();
		} catch (e) {
			console.error('Failed to delete agent:', e);
		}
	}

	// Format log timestamp
	function formatLogTime(date: Date): string {
		return date.toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}

	// Format log entry
	function formatLog(log: AgentLogEntry): string {
		return `[${formatLogTime(log.timestamp)}] ${log.message}`;
	}
</script>

{#if mobile}
	<!-- Mobile: Full-screen card with no BaseCard wrapper -->
	<!-- MobileWorkspace provides the header with title and close button -->
	<div class="agent-content mobile">
		{#if loading}
			<div class="loading-state">
				<Loader2 size={24} class="animate-spin text-muted-foreground" />
				<span class="text-sm text-muted-foreground">Loading agent...</span>
			</div>
		{:else if error}
			<div class="error-state">
				<AlertCircle size={24} class="text-destructive" />
				<span class="text-sm text-destructive">{error}</span>
				<button class="retry-btn" onclick={fetchAgent}>Retry</button>
			</div>
		{:else if agent}
			<!-- Status Header -->
			<div class="status-header">
				<div class="status-left">
					<span class="status-indicator {statusConfig[agent.status].color}">
						{#if agent.status === 'running'}
							<Loader2 size={14} class="animate-spin" />
						{:else if agent.status === 'completed'}
							<CheckCircle size={14} />
						{:else if agent.status === 'failed'}
							<AlertCircle size={14} />
						{:else if agent.status === 'paused'}
							<Pause size={14} />
						{:else}
							<Circle size={14} />
						{/if}
						<span>{statusConfig[agent.status].label}</span>
					</span>
					{#if agent.status === 'running'}
						<span class="duration">{duration}</span>
						{#if timeRemaining !== null}
							<span class="time-remaining" title="Time remaining">({timeRemaining} left)</span>
						{:else}
							<span class="time-remaining unlimited" title="Unlimited duration">(Unlimited)</span>
						{/if}
					{/if}
				</div>

				<div class="header-right">
					{#if agent.prUrl}
						<a
							href={agent.prUrl}
							target="_blank"
							rel="noopener noreferrer"
							class="pr-badge"
							title="View Pull Request"
						>
							<ExternalLink size={12} />
							<span>PR</span>
						</a>
					{/if}
					{#if agent.branch}
						<div class="branch-badge">
							<GitBranch size={12} />
							<span>{agent.branch}</span>
						</div>
					{/if}
				</div>
			</div>

			<!-- Progress Bar -->
			{#if agent.progress !== undefined && agent.status === 'running'}
				<div class="progress-container">
					<div class="progress-bar" style:width="{agent.progress}%"></div>
				</div>
			{/if}

			<!-- Error Message -->
			{#if agent.status === 'failed' && agent.error}
				<div class="error-banner">
					<AlertCircle size={14} />
					<span>{agent.error}</span>
				</div>
			{/if}

			<!-- Result Summary -->
			{#if agent.status === 'completed' && agent.resultSummary}
				<div class="result-banner">
					<CheckCircle size={14} />
					<span>{agent.resultSummary}</span>
				</div>
			{/if}

			<!-- Task Tree -->
			{#if agent.tasks && agent.tasks.length > 0}
				<div class="task-tree">
					<div class="section-label">Tasks</div>
					<div class="tasks">
						{#each agent.tasks as task}
							{@const TaskIcon = taskStatusIcons[task.status]}
							{@const hasChildren = task.children && task.children.length > 0}
							{@const isExpanded = expandedTasks.has(task.id)}

							<div class="task-item">
								<button
									class="task-row"
									onclick={() => hasChildren && toggleTask(task.id)}
									class:has-children={hasChildren}
								>
									{#if hasChildren}
										{#if isExpanded}
											<ChevronDown size={14} class="expand-icon" />
										{:else}
											<ChevronRight size={14} class="expand-icon" />
										{/if}
									{:else}
										<span class="expand-placeholder"></span>
									{/if}
									<TaskIcon
										size={14}
										class="task-icon {task.status === 'in_progress' ? 'animate-spin' : ''} {task.status === 'completed' ? 'task-completed' : task.status === 'failed' ? 'task-failed' : ''}"
									/>
									<span class="task-title">{task.name}</span>
								</button>

								{#if hasChildren && isExpanded}
									<div class="task-children">
										{#each task.children as child}
											{@const ChildIcon = taskStatusIcons[child.status]}
											<div class="task-row child">
												<span class="expand-placeholder"></span>
												<ChildIcon
													size={12}
													class="task-icon {child.status === 'in_progress' ? 'animate-spin' : ''} {child.status === 'completed' ? 'task-completed' : child.status === 'failed' ? 'task-failed' : ''}"
												/>
												<span class="task-title">{child.name}</span>
											</div>
										{/each}
									</div>
								{/if}
							</div>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Log Lines -->
			{#if agent.logs && agent.logs.length > 0}
				<div class="logs-section">
					<div class="section-label">Recent Logs</div>
					<div class="logs">
						{#each agent.logs.slice(-4) as log}
							<div class="log-line">{formatLog(log)}</div>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Control Buttons -->
			<div class="controls">
				{#if agent.status === 'running'}
					<button class="control-btn pause" onclick={handlePause}>
						<Pause size={14} />
						<span>Pause</span>
					</button>
				{:else if agent.status === 'paused'}
					<button class="control-btn resume" onclick={handleResume}>
						<Play size={14} />
						<span>Resume</span>
					</button>
				{/if}

				{#if agent.status === 'running' || agent.status === 'paused' || agent.status === 'queued'}
					<button class="control-btn cancel" onclick={handleCancel}>
						<Square size={14} />
						<span>Cancel</span>
					</button>
				{/if}

				{#if agent.status === 'completed' || agent.status === 'failed'}
					<button class="control-btn delete" onclick={handleDelete}>
						<Trash2 size={14} />
						<span>Delete</span>
					</button>
				{/if}
			</div>
		{/if}
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="agent-content">
			{#if loading}
				<div class="loading-state">
					<Loader2 size={24} class="animate-spin text-muted-foreground" />
					<span class="text-sm text-muted-foreground">Loading agent...</span>
				</div>
			{:else if error}
				<div class="error-state">
					<AlertCircle size={24} class="text-destructive" />
					<span class="text-sm text-destructive">{error}</span>
					<button class="retry-btn" onclick={fetchAgent}>Retry</button>
				</div>
			{:else if agent}
				<!-- Status Header -->
				<div class="status-header">
					<div class="status-left">
						<span class="status-indicator {statusConfig[agent.status].color}">
							{#if agent.status === 'running'}
								<Loader2 size={14} class="animate-spin" />
							{:else if agent.status === 'completed'}
								<CheckCircle size={14} />
							{:else if agent.status === 'failed'}
								<AlertCircle size={14} />
							{:else if agent.status === 'paused'}
								<Pause size={14} />
							{:else}
								<Circle size={14} />
							{/if}
							<span>{statusConfig[agent.status].label}</span>
						</span>
						{#if agent.status === 'running'}
							<span class="duration">{duration}</span>
							{#if timeRemaining !== null}
								<span class="time-remaining" title="Time remaining">({timeRemaining} left)</span>
							{:else}
								<span class="time-remaining unlimited" title="Unlimited duration">(Unlimited)</span>
							{/if}
						{/if}
					</div>

					<div class="header-right">
						{#if agent.prUrl}
							<a
								href={agent.prUrl}
								target="_blank"
								rel="noopener noreferrer"
								class="pr-badge"
								title="View Pull Request"
							>
								<ExternalLink size={12} />
								<span>PR</span>
							</a>
						{/if}
						{#if agent.branch}
							<div class="branch-badge">
								<GitBranch size={12} />
								<span>{agent.branch}</span>
							</div>
						{/if}
					</div>
				</div>

				<!-- Progress Bar -->
				{#if agent.progress !== undefined && agent.status === 'running'}
					<div class="progress-container">
						<div class="progress-bar" style:width="{agent.progress}%"></div>
					</div>
				{/if}

				<!-- Error Message -->
				{#if agent.status === 'failed' && agent.error}
					<div class="error-banner">
						<AlertCircle size={14} />
						<span>{agent.error}</span>
					</div>
				{/if}

				<!-- Result Summary -->
				{#if agent.status === 'completed' && agent.resultSummary}
					<div class="result-banner">
						<CheckCircle size={14} />
						<span>{agent.resultSummary}</span>
					</div>
				{/if}

				<!-- Task Tree -->
				{#if agent.tasks && agent.tasks.length > 0}
					<div class="task-tree">
						<div class="section-label">Tasks</div>
						<div class="tasks">
							{#each agent.tasks as task}
								{@const TaskIcon = taskStatusIcons[task.status]}
								{@const hasChildren = task.children && task.children.length > 0}
								{@const isExpanded = expandedTasks.has(task.id)}

								<div class="task-item">
									<button
										class="task-row"
										onclick={() => hasChildren && toggleTask(task.id)}
										class:has-children={hasChildren}
									>
										{#if hasChildren}
											{#if isExpanded}
												<ChevronDown size={14} class="expand-icon" />
											{:else}
												<ChevronRight size={14} class="expand-icon" />
											{/if}
										{:else}
											<span class="expand-placeholder"></span>
										{/if}
										<TaskIcon
											size={14}
											class="task-icon {task.status === 'in_progress' ? 'animate-spin' : ''} {task.status === 'completed' ? 'task-completed' : task.status === 'failed' ? 'task-failed' : ''}"
										/>
										<span class="task-title">{task.name}</span>
									</button>

									{#if hasChildren && isExpanded}
										<div class="task-children">
											{#each task.children as child}
												{@const ChildIcon = taskStatusIcons[child.status]}
												<div class="task-row child">
													<span class="expand-placeholder"></span>
													<ChildIcon
														size={12}
														class="task-icon {child.status === 'in_progress' ? 'animate-spin' : ''} {child.status === 'completed' ? 'task-completed' : child.status === 'failed' ? 'task-failed' : ''}"
													/>
													<span class="task-title">{child.name}</span>
												</div>
											{/each}
										</div>
									{/if}
								</div>
							{/each}
						</div>
					</div>
				{/if}

				<!-- Log Lines -->
				{#if agent.logs && agent.logs.length > 0}
					<div class="logs-section">
						<div class="section-label">Recent Logs</div>
						<div class="logs">
							{#each agent.logs.slice(-4) as log}
								<div class="log-line">{formatLog(log)}</div>
							{/each}
						</div>
					</div>
				{/if}

				<!-- Control Buttons -->
				<div class="controls">
					{#if agent.status === 'running'}
						<button class="control-btn pause" onclick={handlePause}>
							<Pause size={14} />
							<span>Pause</span>
						</button>
					{:else if agent.status === 'paused'}
						<button class="control-btn resume" onclick={handleResume}>
							<Play size={14} />
							<span>Resume</span>
						</button>
					{/if}

					{#if agent.status === 'running' || agent.status === 'paused' || agent.status === 'queued'}
						<button class="control-btn cancel" onclick={handleCancel}>
							<Square size={14} />
							<span>Cancel</span>
						</button>
					{/if}

					{#if agent.status === 'completed' || agent.status === 'failed'}
						<button class="control-btn delete" onclick={handleDelete}>
							<Trash2 size={14} />
							<span>Delete</span>
						</button>
					{/if}
				</div>
			{/if}
		</div>
	</BaseCard>
{/if}

<style>
	/* ============================================
	   MONITOR STYLES
	   ============================================ */
	.agent-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
		background: var(--card);
	}

	.agent-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* Loading/Error States */
	.loading-state,
	.error-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		height: 100%;
		padding: 24px;
	}

	.retry-btn {
		padding: 8px 16px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 8px;
		font-size: 0.75rem;
		color: var(--foreground);
		cursor: pointer;
		transition: all 0.15s ease;
		box-shadow: var(--shadow-s);
	}

	.retry-btn:hover {
		background: var(--accent);
		box-shadow: var(--shadow-m);
	}

	/* Status Header */
	.status-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 14px 16px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 30%, transparent);
		flex-shrink: 0;
	}

	.status-left {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.header-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.status-indicator {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.8125rem;
		font-weight: 600;
		padding: 6px 12px;
		border-radius: 8px;
		box-shadow: var(--shadow-s);
	}

	/* Status color classes */
	.status-indicator.status-running {
		background: color-mix(in oklch, var(--info) 15%, transparent);
		color: var(--info);
		border: 1px solid color-mix(in oklch, var(--info) 30%, transparent);
	}

	.status-indicator.status-warning {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		color: var(--warning);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
	}

	.status-indicator.status-muted {
		background: var(--muted);
		color: var(--muted-foreground);
		border: 1px solid var(--border);
	}

	.status-indicator.status-destructive {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.status-indicator.status-success {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border: 1px solid color-mix(in oklch, var(--success) 30%, transparent);
	}

	.duration {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		font-family: monospace;
		padding: 4px 8px;
		background: var(--muted);
		border-radius: 6px;
	}

	.time-remaining {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-family: monospace;
	}

	.time-remaining.unlimited {
		color: var(--primary);
	}

	.branch-badge,
	.pr-badge {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 5px 10px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 6px;
		font-size: 0.6875rem;
		color: var(--foreground);
		text-decoration: none;
		font-weight: 500;
		transition: all 0.15s ease;
	}

	.branch-badge:hover {
		background: var(--accent);
	}

	.pr-badge {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		border-color: color-mix(in oklch, var(--success) 30%, transparent);
		color: var(--success);
	}

	.pr-badge:hover {
		background: color-mix(in oklch, var(--success) 25%, transparent);
	}

	/* Progress Bar */
	.progress-container {
		height: 4px;
		background: var(--muted);
		flex-shrink: 0;
	}

	.progress-bar {
		height: 100%;
		background: linear-gradient(90deg, var(--primary), var(--info));
		transition: width 0.3s ease;
		box-shadow: 0 0 8px color-mix(in oklch, var(--primary) 50%, transparent);
	}

	/* Error/Result Banners */
	.error-banner,
	.result-banner {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 16px;
		font-size: 0.8125rem;
		flex-shrink: 0;
		font-weight: 500;
	}

	.error-banner {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive);
		border-bottom: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	.result-banner {
		background: color-mix(in oklch, var(--success) 12%, transparent);
		color: var(--success);
		border-bottom: 1px solid color-mix(in oklch, var(--success) 25%, transparent);
	}

	/* Task Tree */
	.task-tree {
		flex: 1;
		overflow-y: auto;
		padding: 14px 16px;
	}

	.section-label {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin-bottom: 10px;
	}

	.tasks {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.task-item {
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--muted) 30%, transparent);
		border-radius: 8px;
		border: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
		overflow: hidden;
	}

	.task-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		background: transparent;
		border: none;
		color: var(--foreground);
		font-size: 0.8125rem;
		text-align: left;
		width: 100%;
		cursor: default;
		transition: background-color 0.15s ease;
	}

	.task-row.has-children {
		cursor: pointer;
	}

	.task-row.has-children:hover {
		background: color-mix(in oklch, var(--accent) 60%, transparent);
	}

	.task-row.child {
		padding-left: 28px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 20%, transparent);
		border-top: 1px solid color-mix(in oklch, var(--border) 30%, transparent);
	}

	.task-row.child:hover {
		background: color-mix(in oklch, var(--accent) 40%, transparent);
	}

	.expand-icon {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.expand-placeholder {
		width: 14px;
		flex-shrink: 0;
	}

	.task-icon {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	/* Task status icon colors */
	:global(.task-completed) {
		color: var(--success) !important;
	}

	:global(.task-failed) {
		color: var(--destructive) !important;
	}

	.task-title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.task-children {
		display: flex;
		flex-direction: column;
	}

	/* Logs Section */
	.logs-section {
		padding: 14px 16px;
		border-top: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 20%, transparent);
		flex-shrink: 0;
	}

	.logs {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 10px 12px;
		background: oklch(0.12 0.008 260);
		border-radius: 8px;
		border: 1px solid var(--border);
	}

	.log-line {
		font-family: monospace;
		font-size: 0.6875rem;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		opacity: 0.85;
	}

	/* Controls */
	.controls {
		display: flex;
		gap: 10px;
		padding: 14px 16px;
		border-top: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		flex-shrink: 0;
	}

	.control-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 10px 18px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 8px;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		cursor: pointer;
		transition: all 0.15s ease;
		box-shadow: var(--shadow-s);
	}

	.control-btn:hover {
		background: var(--accent);
		box-shadow: var(--shadow-m);
		transform: translateY(-1px);
	}

	.control-btn:active {
		transform: translateY(0);
	}

	.control-btn.cancel:hover,
	.control-btn.delete:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 40%, transparent);
		color: var(--destructive);
	}

	.control-btn.resume:hover {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: color-mix(in oklch, var(--primary) 40%, transparent);
		color: var(--primary);
	}

	.control-btn.pause:hover {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		border-color: color-mix(in oklch, var(--warning) 40%, transparent);
		color: var(--warning);
	}

	/* Utility classes */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}
</style>
