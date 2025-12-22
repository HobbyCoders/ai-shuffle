<script lang="ts">
	/**
	 * AgentCard - Agent monitoring card for The Deck
	 *
	 * Features:
	 * - Status header with duration timer
	 * - Branch badge (if exists)
	 * - Mini task tree (collapsible)
	 * - Last few log lines
	 * - Control buttons: Pause, Resume, Cancel
	 * - Progress bar
	 */

	import { onMount, onDestroy } from 'svelte';
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
		type BackgroundAgent,
		type AgentTask,
		type AgentLogEntry
	} from '$lib/stores/agents';

	interface Props {
		card: DeckCard;
		agentId: string;
		onClose: () => void;
		onMinimize: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
	}

	let {
		card,
		agentId,
		onClose,
		onMinimize,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd
	}: Props = $props();

	// Agent data from store
	let agent = $state<BackgroundAgent | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Task tree expanded state
	let expandedTasks = $state<Set<string>>(new Set());

	// Duration timer
	let duration = $state('0:00');
	let durationInterval: ReturnType<typeof setInterval> | null = null;

	// Time remaining for timeout
	let timeRemaining = $state<string | null>(null);

	// Fetch agent data
	async function fetchAgent() {
		loading = true;
		error = null;
		try {
			const fetchedAgent = await agents.fetchAgent(agentId);
			if (fetchedAgent) {
				agent = fetchedAgent;
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
				// Update agent with logs
				agent = agents.getAgent(agentId) || agent;
			} else {
				error = 'Agent not found';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load agent';
		} finally {
			loading = false;
		}
	}

	// Subscribe to agent updates via store
	onMount(() => {
		fetchAgent();
		agents.subscribeToAgent(agentId);

		// Poll for updates every 5 seconds as backup
		// Only update if meaningful data changed to avoid unnecessary re-renders
		const pollInterval = setInterval(() => {
			const updated = agents.getAgent(agentId);
			if (updated && agent) {
				// Only update if status, progress, or logs changed
				const hasChanged =
					updated.status !== agent.status ||
					updated.progress !== agent.progress ||
					updated.error !== agent.error ||
					updated.prUrl !== agent.prUrl ||
					(updated.logs?.length || 0) !== (agent.logs?.length || 0) ||
					(updated.tasks?.length || 0) !== (agent.tasks?.length || 0);

				if (hasChanged) {
					agent = updated;
				}
			} else if (updated && !agent) {
				agent = updated;
			}
		}, 5000);

		return () => {
			clearInterval(pollInterval);
		};
	});

	onDestroy(() => {
		agents.unsubscribeFromAgent(agentId);
		if (durationInterval) {
			clearInterval(durationInterval);
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

	// Status colors and icons
	const statusConfig = {
		running: { color: 'text-blue-500', label: 'Running' },
		paused: { color: 'text-yellow-500', label: 'Paused' },
		queued: { color: 'text-muted-foreground', label: 'Queued' },
		failed: { color: 'text-destructive', label: 'Failed' },
		completed: { color: 'text-green-500', label: 'Completed' }
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

	// Control actions
	async function handlePause() {
		if (!agent) return;
		try {
			await agents.pauseAgent(agent.id);
			agent = agents.getAgent(agentId) || agent;
		} catch (e) {
			console.error('Failed to pause agent:', e);
		}
	}

	async function handleResume() {
		if (!agent) return;
		try {
			await agents.resumeAgent(agent.id);
			agent = agents.getAgent(agentId) || agent;
		} catch (e) {
			console.error('Failed to resume agent:', e);
		}
	}

	async function handleCancel() {
		if (!agent) return;
		try {
			await agents.cancelAgent(agent.id);
			agent = agents.getAgent(agentId) || agent;
		} catch (e) {
			console.error('Failed to cancel agent:', e);
		}
	}

	async function handleDelete() {
		if (!agent) return;
		try {
			await agents.deleteAgent(agent.id);
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

<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
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
										class="task-icon {task.status === 'in_progress' ? 'animate-spin' : ''} {task.status === 'completed' ? 'text-green-500' : task.status === 'failed' ? 'text-destructive' : ''}"
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
													class="task-icon {child.status === 'in_progress' ? 'animate-spin' : ''} {child.status === 'completed' ? 'text-green-500' : child.status === 'failed' ? 'text-destructive' : ''}"
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

<style>
	.agent-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
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
		padding: 6px 12px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 4px;
		font-size: 0.75rem;
		color: hsl(var(--foreground));
		cursor: pointer;
	}

	.retry-btn:hover {
		background: hsl(var(--accent));
	}

	/* Status Header */
	.status-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		border-bottom: 1px solid hsl(var(--border) / 0.5);
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
		gap: 6px;
		font-size: 0.8125rem;
		font-weight: 500;
	}

	.duration {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		font-family: monospace;
	}

	.time-remaining {
		font-size: 0.6875rem;
		color: hsl(var(--muted-foreground) / 0.8);
		font-family: monospace;
	}

	.time-remaining.unlimited {
		color: hsl(var(--primary) / 0.7);
	}

	.branch-badge,
	.pr-badge {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 4px 8px;
		background: hsl(var(--muted));
		border-radius: 4px;
		font-size: 0.6875rem;
		color: hsl(var(--muted-foreground));
		text-decoration: none;
	}

	.pr-badge {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}

	.pr-badge:hover {
		background: hsl(var(--primary) / 0.2);
	}

	/* Progress Bar */
	.progress-container {
		height: 3px;
		background: hsl(var(--muted));
		flex-shrink: 0;
	}

	.progress-bar {
		height: 100%;
		background: hsl(var(--primary));
		transition: width 0.3s ease;
	}

	/* Error/Result Banners */
	.error-banner,
	.result-banner {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 16px;
		font-size: 0.75rem;
		flex-shrink: 0;
	}

	.error-banner {
		background: hsl(var(--destructive) / 0.1);
		color: hsl(var(--destructive));
	}

	.result-banner {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}

	/* Task Tree */
	.task-tree {
		flex: 1;
		overflow-y: auto;
		padding: 12px 16px;
	}

	.section-label {
		font-size: 0.6875rem;
		font-weight: 500;
		color: hsl(var(--muted-foreground));
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin-bottom: 8px;
	}

	.tasks {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.task-item {
		display: flex;
		flex-direction: column;
	}

	.task-row {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 4px;
		background: transparent;
		border: none;
		border-radius: 4px;
		color: hsl(var(--foreground));
		font-size: 0.8125rem;
		text-align: left;
		width: 100%;
		cursor: default;
	}

	.task-row.has-children {
		cursor: pointer;
	}

	.task-row.has-children:hover {
		background: hsl(var(--accent) / 0.5);
	}

	.task-row.child {
		padding-left: 20px;
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	.expand-icon {
		color: hsl(var(--muted-foreground));
		flex-shrink: 0;
	}

	.expand-placeholder {
		width: 14px;
		flex-shrink: 0;
	}

	.task-icon {
		flex-shrink: 0;
		color: hsl(var(--muted-foreground));
	}

	.task-title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.task-children {
		display: flex;
		flex-direction: column;
		gap: 2px;
		margin-left: 4px;
	}

	/* Logs Section */
	.logs-section {
		padding: 12px 16px;
		border-top: 1px solid hsl(var(--border) / 0.5);
		flex-shrink: 0;
	}

	.logs {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.log-line {
		font-family: monospace;
		font-size: 0.6875rem;
		color: hsl(var(--muted-foreground));
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Controls */
	.controls {
		display: flex;
		gap: 8px;
		padding: 12px 16px;
		border-top: 1px solid hsl(var(--border) / 0.5);
		flex-shrink: 0;
	}

	.control-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 16px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 6px;
		font-size: 0.8125rem;
		font-weight: 500;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.control-btn:hover {
		background: hsl(var(--accent));
	}

	.control-btn.cancel:hover,
	.control-btn.delete:hover {
		background: hsl(var(--destructive) / 0.1);
		border-color: hsl(var(--destructive) / 0.3);
		color: hsl(var(--destructive));
	}

	.control-btn.resume:hover {
		background: hsl(var(--primary) / 0.1);
		border-color: hsl(var(--primary) / 0.3);
		color: hsl(var(--primary));
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

	:global(.text-green-500) {
		color: rgb(34 197 94);
	}

	:global(.text-blue-500) {
		color: rgb(59 130 246);
	}

	:global(.text-yellow-500) {
		color: rgb(234 179 8);
	}
</style>
