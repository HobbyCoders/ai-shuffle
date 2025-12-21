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
		AlertCircle
	} from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';

	interface AgentTask {
		id: string;
		title: string;
		status: 'pending' | 'running' | 'completed' | 'error';
		children?: AgentTask[];
	}

	interface AgentData {
		status: 'running' | 'paused' | 'idle' | 'error' | 'completed';
		branch?: string;
		startTime?: Date;
		tasks?: AgentTask[];
		logs?: string[];
		progress?: number;
	}

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
		onPause?: () => void;
		onResume?: () => void;
		onCancel?: () => void;
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
		onResizeEnd,
		onPause,
		onResume,
		onCancel
	}: Props = $props();

	// Agent data (would come from store/props in real implementation)
	let agentData = $state<AgentData>({
		status: 'running',
		branch: 'feature/add-cards',
		startTime: new Date(Date.now() - 125000), // 2 min ago
		progress: 45,
		tasks: [
			{
				id: '1',
				title: 'Analyze codebase',
				status: 'completed',
			},
			{
				id: '2',
				title: 'Create components',
				status: 'running',
				children: [
					{ id: '2.1', title: 'BaseCard.svelte', status: 'completed' },
					{ id: '2.2', title: 'AgentCard.svelte', status: 'running' },
					{ id: '2.3', title: 'StudioCard.svelte', status: 'pending' },
				],
			},
			{
				id: '3',
				title: 'Run tests',
				status: 'pending',
			},
		],
		logs: [
			'[14:32:15] Starting task analysis...',
			'[14:32:18] Found 12 files to modify',
			'[14:32:20] Creating BaseCard.svelte...',
			'[14:34:45] Creating AgentCard.svelte...',
		],
	});

	// Task tree expanded state
	let expandedTasks = $state<Set<string>>(new Set(['2']));

	// Duration timer
	let duration = $state('0:00');

	$effect(() => {
		if (agentData.status !== 'running' || !agentData.startTime) return;

		function updateDuration() {
			if (!agentData.startTime) return;
			const elapsed = Math.floor((Date.now() - agentData.startTime.getTime()) / 1000);
			const mins = Math.floor(elapsed / 60);
			const secs = elapsed % 60;
			duration = `${mins}:${secs.toString().padStart(2, '0')}`;
		}

		updateDuration();
		const interval = setInterval(updateDuration, 1000);

		return () => clearInterval(interval);
	});

	// Status colors and icons
	const statusConfig: Record<AgentData['status'], { color: string; label: string }> = {
		running: { color: 'text-blue-500', label: 'Running' },
		paused: { color: 'text-yellow-500', label: 'Paused' },
		idle: { color: 'text-muted-foreground', label: 'Idle' },
		error: { color: 'text-destructive', label: 'Error' },
		completed: { color: 'text-green-500', label: 'Completed' },
	};

	const taskStatusIcons = {
		pending: Circle,
		running: Loader2,
		completed: CheckCircle,
		error: AlertCircle,
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
	function handlePause() {
		agentData.status = 'paused';
		onPause?.();
	}

	function handleResume() {
		agentData.status = 'running';
		onResume?.();
	}

	function handleCancel() {
		agentData.status = 'idle';
		onCancel?.();
	}
</script>

<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
	<div class="agent-content">
		<!-- Status Header -->
		<div class="status-header">
			<div class="status-left">
				<span class="status-indicator {statusConfig[agentData.status].color}">
					{#if agentData.status === 'running'}
						<Loader2 size={14} class="animate-spin" />
					{:else if agentData.status === 'completed'}
						<CheckCircle size={14} />
					{:else if agentData.status === 'error'}
						<AlertCircle size={14} />
					{:else}
						<Circle size={14} />
					{/if}
					<span>{statusConfig[agentData.status].label}</span>
				</span>
				{#if agentData.status === 'running'}
					<span class="duration">{duration}</span>
				{/if}
			</div>

			{#if agentData.branch}
				<div class="branch-badge">
					<GitBranch size={12} />
					<span>{agentData.branch}</span>
				</div>
			{/if}
		</div>

		<!-- Progress Bar -->
		{#if agentData.progress !== undefined && agentData.status === 'running'}
			<div class="progress-container">
				<div class="progress-bar" style:width="{agentData.progress}%"></div>
			</div>
		{/if}

		<!-- Task Tree -->
		{#if agentData.tasks && agentData.tasks.length > 0}
			<div class="task-tree">
				<div class="section-label">Tasks</div>
				<div class="tasks">
					{#each agentData.tasks as task}
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
									class="task-icon {task.status === 'running' ? 'animate-spin' : ''} {task.status === 'completed' ? 'text-green-500' : task.status === 'error' ? 'text-destructive' : ''}"
								/>
								<span class="task-title">{task.title}</span>
							</button>

							{#if hasChildren && isExpanded}
								<div class="task-children">
									{#each task.children as child}
										{@const ChildIcon = taskStatusIcons[child.status]}
										<div class="task-row child">
											<span class="expand-placeholder"></span>
											<ChildIcon
												size={12}
												class="task-icon {child.status === 'running' ? 'animate-spin' : ''} {child.status === 'completed' ? 'text-green-500' : child.status === 'error' ? 'text-destructive' : ''}"
											/>
											<span class="task-title">{child.title}</span>
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
		{#if agentData.logs && agentData.logs.length > 0}
			<div class="logs-section">
				<div class="section-label">Recent Logs</div>
				<div class="logs">
					{#each agentData.logs.slice(-4) as log}
						<div class="log-line">{log}</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Control Buttons -->
		<div class="controls">
			{#if agentData.status === 'running'}
				<button class="control-btn pause" onclick={handlePause}>
					<Pause size={14} />
					<span>Pause</span>
				</button>
			{:else if agentData.status === 'paused'}
				<button class="control-btn resume" onclick={handleResume}>
					<Play size={14} />
					<span>Resume</span>
				</button>
			{/if}

			{#if agentData.status === 'running' || agentData.status === 'paused'}
				<button class="control-btn cancel" onclick={handleCancel}>
					<Square size={14} />
					<span>Cancel</span>
				</button>
			{/if}
		</div>
	</div>
</BaseCard>

<style>
	.agent-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
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

	.branch-badge {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 4px 8px;
		background: hsl(var(--muted));
		border-radius: 4px;
		font-size: 0.6875rem;
		color: hsl(var(--muted-foreground));
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

	.control-btn.cancel:hover {
		background: hsl(var(--destructive) / 0.1);
		border-color: hsl(var(--destructive) / 0.3);
		color: hsl(var(--destructive));
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
