<script lang="ts">
	/**
	 * AgentCard - Background Agent Launch & Monitor Card
	 *
	 * Two modes:
	 * 1. LAUNCH MODE (no agentId): Configure and launch a new background agent
	 *    - Profile/Project selectors
	 *    - Branch selector with new branch creation
	 *    - Auto-PR, Auto-Merge, Max Duration settings
	 *    - Input island for prompt
	 *
	 * 2. MONITOR MODE (with agentId): Monitor an existing background agent
	 *    - Status display with duration timer
	 *    - Task tree with expansion
	 *    - Recent logs
	 *    - Control buttons (pause/resume/cancel/delete)
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
		Trash2,
		Plus,
		Bot
	} from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import AgentInput from './AgentInput.svelte';
	import type { DeckCard } from './types';
	import {
		agents,
		allAgents,
		type BackgroundAgent,
		type AgentTask,
		type AgentLogEntry
	} from '$lib/stores/agents';
	import { profiles, projects, type Project } from '$lib/stores/tabs';
	import { git, branches as gitBranches, currentBranch, gitLoading } from '$lib/stores/git';
	import type { Profile } from '$lib/api/client';
	import type { FileUploadResponse } from '$lib/api/client';

	interface Props {
		card: DeckCard;
		agentId?: string | null;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		onAgentLaunched?: (agentId: string) => void;
		mobile?: boolean;
	}

	let {
		card,
		agentId = null,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		onAgentLaunched,
		mobile = false
	}: Props = $props();

	// ============================================
	// MODE DETECTION
	// ============================================
	const isLaunchMode = $derived(!agentId);

	// ============================================
	// LAUNCH MODE STATE
	// ============================================
	let selectedProfile = $state<string | null>(null);
	let selectedProject = $state<string | null>(null);
	let selectedBranch = $state('main');
	let createNewBranch = $state(false);
	let newBranchName = $state('');
	let showNewBranchInput = $state(false);
	let autoPR = $state(true);
	let autoMerge = $state(false);
	let maxDurationMinutes = $state(30);
	let isLaunching = $state(false);
	let launchError = $state<string | null>(null);

	const durationOptions = [
		{ value: 15, label: '15 min' },
		{ value: 30, label: '30 min' },
		{ value: 60, label: '1 hour' },
		{ value: 120, label: '2 hours' },
		{ value: 0, label: 'Unlimited' }
	];

	// Load git branches when project changes
	$effect(() => {
		if (selectedProject && isLaunchMode) {
			git.loadRepository(selectedProject);
		}
	});

	// Get available branches for selector
	const availableBranches = $derived(
		selectedProject ? $gitBranches.filter(b => !b.remote).map(b => b.name) : ['main']
	);

	function handleBranchChange(value: string) {
		if (value === '__new__') {
			showNewBranchInput = true;
		} else {
			selectedBranch = value;
			createNewBranch = false;
		}
	}

	function handleNewBranchCreate() {
		if (newBranchName.trim()) {
			selectedBranch = newBranchName.trim();
			createNewBranch = true;
			showNewBranchInput = false;
			newBranchName = '';
		}
	}

	function handleNewBranchCancel() {
		showNewBranchInput = false;
		newBranchName = '';
	}

	async function handleLaunchAgent(prompt: string, files: FileUploadResponse[]) {
		if (!selectedProfile || !selectedProject || !prompt.trim()) {
			launchError = 'Please select a profile and project';
			return;
		}

		isLaunching = true;
		launchError = null;

		try {
			const newAgent = await agents.launchAgent({
				prompt,
				profileId: selectedProfile,
				projectId: selectedProject,
				branch: selectedBranch,
				createNewBranch,
				autoPR,
				autoMerge,
				maxDurationMinutes: maxDurationMinutes || undefined
			});

			if (newAgent) {
				onAgentLaunched?.(newAgent.id);
			}
		} catch (e) {
			console.error('Failed to launch agent:', e);
			launchError = e instanceof Error ? e.message : 'Failed to launch agent';
		} finally {
			isLaunching = false;
		}
	}

	// ============================================
	// MONITOR MODE STATE
	// ============================================
	const agent = $derived.by(() => {
		if (!agentId) return null;
		const agentsList = $allAgents;
		return agentsList.find(a => a.id === agentId) || null;
	});

	let initialFetchDone = $state(false);
	let fetchError = $state<string | null>(null);
	const loading = $derived(!agent && !initialFetchDone && !isLaunchMode);
	const error = $derived(fetchError);

	let expandedTasks = $state<Set<string>>(new Set());
	let duration = $state('0:00');
	let durationInterval: ReturnType<typeof setInterval> | null = null;
	let timeRemaining = $state<string | null>(null);
	let logRefreshInterval: ReturnType<typeof setInterval> | null = null;

	async function fetchAgent() {
		if (!agentId) return;
		fetchError = null;
		try {
			const fetchedAgent = await agents.fetchAgent(agentId);
			if (fetchedAgent) {
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
				await agents.fetchLogs(agentId, { limit: 10 });
			} else {
				fetchError = 'Agent not found';
			}
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load agent';
		} finally {
			initialFetchDone = true;
		}
	}

	$effect(() => {
		if (agentId && !initialFetchDone && !isLaunchMode) {
			fetchAgent();
			agents.subscribeToAgent(agentId);
		}
	});

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
			const currentExpanded = untrack(() => expandedTasks);
			const merged = new Set([...currentExpanded, ...runningTaskIds]);
			if (merged.size !== currentExpanded.size) {
				expandedTasks = merged;
			}
		}
	});

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
					console.error('Failed to refresh logs:', e);
				}
			}, 3000);
		}
		return () => {
			if (logRefreshInterval) {
				clearInterval(logRefreshInterval);
				logRefreshInterval = null;
			}
		};
	});

	onDestroy(() => {
		if (agentId) {
			agents.unsubscribeFromAgent(agentId);
		}
		if (durationInterval) clearInterval(durationInterval);
		if (logRefreshInterval) clearInterval(logRefreshInterval);
	});

	$effect(() => {
		if (durationInterval) {
			clearInterval(durationInterval);
			durationInterval = null;
		}
		if (agent?.status !== 'running' || !agent?.startedAt) return;

		function updateDuration() {
			if (!agent?.startedAt) return;
			const elapsed = Math.floor((Date.now() - agent.startedAt.getTime()) / 1000);
			const mins = Math.floor(elapsed / 60);
			const secs = elapsed % 60;
			duration = `${mins}:${secs.toString().padStart(2, '0')}`;

			const maxDur = agent?.maxDurationMinutes;
			if (maxDur && maxDur > 0) {
				const remaining = maxDur * 60 - elapsed;
				if (remaining > 0) {
					const remainingMins = Math.floor(remaining / 60);
					const remainingSecs = remaining % 60;
					timeRemaining = `${remainingMins}:${remainingSecs.toString().padStart(2, '0')}`;
				} else {
					timeRemaining = '0:00';
				}
			} else {
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

	function toggleTask(taskId: string) {
		if (expandedTasks.has(taskId)) {
			expandedTasks.delete(taskId);
			expandedTasks = new Set(expandedTasks);
		} else {
			expandedTasks.add(taskId);
			expandedTasks = new Set(expandedTasks);
		}
	}

	async function handlePause() {
		if (!agent || !agentId) return;
		try { await agents.pauseAgent(agentId); } catch (e) { console.error('Failed to pause agent:', e); }
	}

	async function handleResume() {
		if (!agent || !agentId) return;
		try { await agents.resumeAgent(agentId); } catch (e) { console.error('Failed to resume agent:', e); }
	}

	async function handleCancel() {
		if (!agent || !agentId) return;
		try { await agents.cancelAgent(agentId); } catch (e) { console.error('Failed to cancel agent:', e); }
	}

	async function handleDelete() {
		if (!agent || !agentId) return;
		try { await agents.deleteAgent(agentId); onClose(); } catch (e) { console.error('Failed to delete agent:', e); }
	}

	function formatLogTime(date: Date): string {
		return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
	}

	function formatLog(log: AgentLogEntry): string {
		return `[${formatLogTime(log.timestamp)}] ${log.message}`;
	}
</script>

{#snippet launchView()}
	<div class="launch-content">
		<!-- Config Header -->
		<div class="config-header">
			<Bot size={18} class="text-primary" />
			<span class="config-title">Launch Background Agent</span>
		</div>

		<!-- Config Form -->
		<div class="config-form">
			<!-- Profile Selector -->
			<div class="config-group">
				<label for="agent-profile" class="config-label">Profile</label>
				<select
					id="agent-profile"
					bind:value={selectedProfile}
					class="config-select"
				>
					<option value={null} disabled>Select Profile</option>
					{#each $profiles as profile}
						<option value={profile.id}>{profile.name}</option>
					{/each}
				</select>
			</div>

			<!-- Project Selector -->
			<div class="config-group">
				<label for="agent-project" class="config-label">Project</label>
				<select
					id="agent-project"
					bind:value={selectedProject}
					class="config-select"
				>
					<option value={null} disabled>Select Project</option>
					{#each $projects as project}
						<option value={project.id}>{project.name}</option>
					{/each}
				</select>
			</div>

			<!-- Branch Selector -->
			<div class="config-group">
				<label for="agent-branch" class="config-label">
					<GitBranch size={14} />
					Branch
				</label>
				{#if showNewBranchInput}
					<div class="new-branch-input">
						<input
							type="text"
							bind:value={newBranchName}
							placeholder="feature/my-branch"
							class="config-input"
						/>
						<button type="button" class="btn-sm btn-primary" onclick={handleNewBranchCreate} disabled={!newBranchName.trim()}>Create</button>
						<button type="button" class="btn-sm btn-ghost" onclick={handleNewBranchCancel}>Cancel</button>
					</div>
				{:else}
					<div class="branch-row">
						<select
							id="agent-branch"
							value={selectedBranch}
							onchange={(e) => handleBranchChange((e.target as HTMLSelectElement).value)}
							class="config-select"
							disabled={$gitLoading || !selectedProject}
						>
							{#if $gitLoading}
								<option value="">Loading...</option>
							{:else}
								{#each availableBranches as branch}
									<option value={branch}>{branch}</option>
								{/each}
								<option value="__new__">+ New Branch</option>
							{/if}
						</select>
						{#if $gitLoading}
							<Loader2 size={16} class="loading-icon" />
						{/if}
					</div>
				{/if}
				{#if createNewBranch}
					<span class="branch-note">Will create from main</span>
				{/if}
			</div>

			<!-- Options Row -->
			<div class="config-row">
				<!-- Auto-PR -->
				<label class="checkbox-label">
					<input type="checkbox" bind:checked={autoPR} class="checkbox-input" />
					<span class="checkbox-box"></span>
					<span class="checkbox-text">Auto-PR</span>
				</label>

				<!-- Auto-Merge -->
				<label class="checkbox-label" class:dangerous={autoMerge}>
					<input type="checkbox" bind:checked={autoMerge} class="checkbox-input" />
					<span class="checkbox-box"></span>
					<span class="checkbox-text">Auto-Merge</span>
				</label>

				<!-- Duration -->
				<div class="duration-select">
					<select bind:value={maxDurationMinutes} class="config-select compact">
						{#each durationOptions as option}
							<option value={option.value}>{option.label}</option>
						{/each}
					</select>
				</div>
			</div>
		</div>

		<!-- Error Display -->
		{#if launchError}
			<div class="launch-error">
				<AlertCircle size={14} />
				<span>{launchError}</span>
			</div>
		{/if}

		<!-- Input Island -->
		<div class="input-area">
			<AgentInput
				projectId={selectedProject}
				disabled={isLaunching || !selectedProfile || !selectedProject}
				placeholder="Describe the task for the background agent..."
				onSubmit={handleLaunchAgent}
			/>
		</div>

		{#if isLaunching}
			<div class="launching-overlay">
				<Loader2 size={24} class="animate-spin" />
				<span>Launching agent...</span>
			</div>
		{/if}
	</div>
{/snippet}

{#snippet monitorView()}
	<div class="monitor-content">
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
							<span class="time-remaining">({timeRemaining} left)</span>
						{:else}
							<span class="time-remaining unlimited">(Unlimited)</span>
						{/if}
					{/if}
				</div>
				<div class="header-right">
					{#if agent.prUrl}
						<a href={agent.prUrl} target="_blank" rel="noopener noreferrer" class="pr-badge">
							<ExternalLink size={12} /><span>PR</span>
						</a>
					{/if}
					{#if agent.branch}
						<div class="branch-badge"><GitBranch size={12} /><span>{agent.branch}</span></div>
					{/if}
				</div>
			</div>

			{#if agent.progress !== undefined && agent.status === 'running'}
				<div class="progress-container"><div class="progress-bar" style:width="{agent.progress}%"></div></div>
			{/if}

			{#if agent.status === 'failed' && agent.error}
				<div class="error-banner"><AlertCircle size={14} /><span>{agent.error}</span></div>
			{/if}

			{#if agent.status === 'completed' && agent.resultSummary}
				<div class="result-banner"><CheckCircle size={14} /><span>{agent.resultSummary}</span></div>
			{/if}

			{#if agent.tasks && agent.tasks.length > 0}
				<div class="task-tree">
					<div class="section-label">Tasks</div>
					<div class="tasks">
						{#each agent.tasks as task}
							{@const TaskIcon = taskStatusIcons[task.status]}
							{@const hasChildren = task.children && task.children.length > 0}
							{@const isExpanded = expandedTasks.has(task.id)}
							<div class="task-item">
								<button class="task-row" onclick={() => hasChildren && toggleTask(task.id)} class:has-children={hasChildren}>
									{#if hasChildren}
										{#if isExpanded}<ChevronDown size={14} class="expand-icon" />{:else}<ChevronRight size={14} class="expand-icon" />{/if}
									{:else}
										<span class="expand-placeholder"></span>
									{/if}
									<TaskIcon size={14} class="task-icon {task.status === 'in_progress' ? 'animate-spin' : ''} {task.status === 'completed' ? 'task-completed' : task.status === 'failed' ? 'task-failed' : ''}" />
									<span class="task-title">{task.name}</span>
								</button>
								{#if hasChildren && isExpanded}
									<div class="task-children">
										{#each task.children as child}
											{@const ChildIcon = taskStatusIcons[child.status]}
											<div class="task-row child">
												<span class="expand-placeholder"></span>
												<ChildIcon size={12} class="task-icon {child.status === 'in_progress' ? 'animate-spin' : ''} {child.status === 'completed' ? 'task-completed' : child.status === 'failed' ? 'task-failed' : ''}" />
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

			<div class="controls">
				{#if agent.status === 'running'}
					<button class="control-btn pause" onclick={handlePause}><Pause size={14} /><span>Pause</span></button>
				{:else if agent.status === 'paused'}
					<button class="control-btn resume" onclick={handleResume}><Play size={14} /><span>Resume</span></button>
				{/if}
				{#if agent.status === 'running' || agent.status === 'paused' || agent.status === 'queued'}
					<button class="control-btn cancel" onclick={handleCancel}><Square size={14} /><span>Cancel</span></button>
				{/if}
				{#if agent.status === 'completed' || agent.status === 'failed'}
					<button class="control-btn delete" onclick={handleDelete}><Trash2 size={14} /><span>Delete</span></button>
				{/if}
			</div>
		{/if}
	</div>
{/snippet}

{#if mobile}
	<div class="agent-content mobile">
		{#if isLaunchMode}
			{@render launchView()}
		{:else}
			{@render monitorView()}
		{/if}
	</div>
{:else}
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="agent-content">
			{#if isLaunchMode}
				{@render launchView()}
			{:else}
				{@render monitorView()}
			{/if}
		</div>
	</BaseCard>
{/if}

<style>
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

	/* ============================================
	   LAUNCH MODE STYLES
	   ============================================ */
	.launch-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.config-header {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 14px 16px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 30%, transparent);
		flex-shrink: 0;
	}

	.config-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.config-form {
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 14px 16px;
		flex: 1;
		overflow-y: auto;
	}

	.config-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.config-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.config-select,
	.config-input {
		width: 100%;
		padding: 8px 12px;
		font-size: 0.8125rem;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 6px;
		transition: border-color 0.15s, box-shadow 0.15s;
	}

	.config-select:hover,
	.config-input:hover {
		border-color: color-mix(in srgb, var(--border) 70%, var(--foreground));
	}

	.config-select:focus,
	.config-input:focus {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 15%, transparent);
	}

	.config-select:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.config-select.compact {
		padding: 6px 10px;
		font-size: 0.75rem;
	}

	.config-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.branch-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.branch-row .config-select { flex: 1; }

	.branch-note {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-style: italic;
	}

	.new-branch-input {
		display: flex;
		gap: 8px;
	}

	.new-branch-input .config-input { flex: 1; }

	.btn-sm {
		padding: 6px 12px;
		font-size: 0.75rem;
		font-weight: 500;
		border-radius: 4px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.btn-primary {
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
	}

	.btn-primary:hover:not(:disabled) { opacity: 0.9; }
	.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

	.btn-ghost {
		background: transparent;
		color: var(--muted-foreground);
		border: 1px solid var(--border);
	}

	.btn-ghost:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	.checkbox-label {
		display: flex;
		align-items: center;
		gap: 6px;
		cursor: pointer;
		user-select: none;
		font-size: 0.75rem;
		color: var(--foreground);
	}

	.checkbox-label.dangerous { color: var(--warning); }

	.checkbox-input {
		position: absolute;
		opacity: 0;
		pointer-events: none;
	}

	.checkbox-box {
		width: 14px;
		height: 14px;
		border: 1.5px solid var(--border);
		border-radius: 3px;
		background: var(--background);
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s;
		flex-shrink: 0;
	}

	.checkbox-input:checked + .checkbox-box {
		background: var(--primary);
		border-color: var(--primary);
	}

	.checkbox-input:checked + .checkbox-box::after {
		content: '';
		width: 3px;
		height: 6px;
		border: solid white;
		border-width: 0 1.5px 1.5px 0;
		transform: rotate(45deg);
		margin-bottom: 1px;
	}

	.checkbox-text { font-size: 0.75rem; }

	.duration-select { margin-left: auto; }

	.launch-error {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 16px;
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive);
		font-size: 0.8125rem;
		border-top: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	.input-area {
		flex-shrink: 0;
		border-top: 1px solid var(--border);
	}

	.launching-overlay {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		background: color-mix(in oklch, var(--card) 90%, transparent);
		backdrop-filter: blur(4px);
		color: var(--foreground);
		font-size: 0.875rem;
	}

	:global(.loading-icon) {
		animation: spin 1s linear infinite;
		color: var(--muted-foreground);
	}

	/* ============================================
	   MONITOR MODE STYLES
	   ============================================ */
	.monitor-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

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
	}

	.retry-btn:hover { background: var(--accent); }

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
	}

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

	.time-remaining.unlimited { color: var(--primary); }

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

	.branch-badge:hover { background: var(--accent); }

	.pr-badge {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		border-color: color-mix(in oklch, var(--success) 30%, transparent);
		color: var(--success);
	}

	.pr-badge:hover { background: color-mix(in oklch, var(--success) 25%, transparent); }

	.progress-container {
		height: 4px;
		background: var(--muted);
		flex-shrink: 0;
	}

	.progress-bar {
		height: 100%;
		background: linear-gradient(90deg, var(--primary), var(--info));
		transition: width 0.3s ease;
	}

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

	.task-row.has-children { cursor: pointer; }
	.task-row.has-children:hover { background: color-mix(in oklch, var(--accent) 60%, transparent); }

	.task-row.child {
		padding-left: 28px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 20%, transparent);
		border-top: 1px solid color-mix(in oklch, var(--border) 30%, transparent);
	}

	.task-row.child:hover { background: color-mix(in oklch, var(--accent) 40%, transparent); }

	.expand-icon { color: var(--muted-foreground); flex-shrink: 0; }
	.expand-placeholder { width: 14px; flex-shrink: 0; }
	.task-icon { flex-shrink: 0; color: var(--muted-foreground); }

	:global(.task-completed) { color: var(--success) !important; }
	:global(.task-failed) { color: var(--destructive) !important; }

	.task-title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.task-children {
		display: flex;
		flex-direction: column;
	}

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
	}

	.control-btn:hover {
		background: var(--accent);
		transform: translateY(-1px);
	}

	.control-btn:active { transform: translateY(0); }

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

	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
