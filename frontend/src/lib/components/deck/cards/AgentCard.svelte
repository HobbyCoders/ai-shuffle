<script lang="ts">
	/**
	 * AgentCard - Agent card for The Deck
	 *
	 * Operates in two modes:
	 * 1. Launcher Mode: When no agentId is provided, shows agent configuration UI
	 * 2. Monitor Mode: When agentId is provided, shows agent monitoring UI
	 *
	 * Work modes (in launcher):
	 * - GitHub: Creates feature branch, commits changes, creates PR on completion
	 * - Local: Works directly on project folder, no git features
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
		Trash2,
		Rocket,
		User,
		FolderKanban,
		Folder
	} from 'lucide-svelte';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import {
		agents,
		type BackgroundAgent,
		type AgentTask,
		type AgentLogEntry
	} from '$lib/stores/agents';
	import { deck } from '$lib/stores/deck';

	interface Props {
		card: DeckCard;
		agentId?: string;
		onClose: () => void;
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
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd
	}: Props = $props();

	// ============================================
	// Mode Detection
	// ============================================
	// Launcher mode: no agentId OR agentId matches card.id (fresh card)
	// Monitor mode: agentId is provided and is a real agent ID
	let mode = $state<'launcher' | 'monitor'>('launcher');
	let actualAgentId = $state<string | null>(null);

	$effect(() => {
		if (agentId && agentId !== card.id) {
			mode = 'monitor';
			actualAgentId = agentId;
		} else {
			mode = 'launcher';
			actualAgentId = null;
		}
	});

	// ============================================
	// LAUNCHER MODE STATE
	// ============================================
	interface Branch {
		name: string;
		is_current: boolean;
	}

	interface Profile {
		id: string;
		name: string;
	}

	interface Project {
		id: string;
		name: string;
	}

	type WorkMode = 'github' | 'local';

	// Form state
	let name = $state('');
	let prompt = $state('');
	let profileId = $state<string | undefined>(undefined);
	let projectId = $state<string | undefined>(undefined);
	let workMode = $state<WorkMode>('github');
	let isLaunching = $state(false);
	let launchError = $state<string | null>(null);

	// Data from API
	let profiles = $state<Profile[]>([]);
	let projects = $state<Project[]>([]);
	let branches = $state<Branch[]>([]);
	let baseBranch = $state<string | undefined>(undefined);
	let loadingProfiles = $state(true);
	let loadingProjects = $state(true);
	let loadingBranches = $state(false);
	let isGitRepo = $state(false);

	// Validation
	const canLaunch = $derived(name.trim().length > 0 && prompt.trim().length > 0 && !isLaunching);

	async function fetchProfiles() {
		try {
			const response = await fetch('/api/v1/profiles', {
				credentials: 'include'
			});
			if (response.ok) {
				const data = await response.json();
				profiles = data.profiles ?? data ?? [];
			}
		} catch (err) {
			console.error('Failed to fetch profiles:', err);
		} finally {
			loadingProfiles = false;
		}
	}

	async function fetchProjects() {
		try {
			const response = await fetch('/api/v1/projects', {
				credentials: 'include'
			});
			if (response.ok) {
				const data = await response.json();
				projects = data.projects ?? data ?? [];
			}
		} catch (err) {
			console.error('Failed to fetch projects:', err);
		} finally {
			loadingProjects = false;
		}
	}

	async function fetchBranches(projId: string) {
		loadingBranches = true;
		branches = [];
		baseBranch = undefined;
		isGitRepo = false;
		try {
			// Only fetch local branches (exclude remote tracking branches like origin/*)
			const response = await fetch(`/api/v1/projects/${projId}/git/branches?include_remote=false`, {
				credentials: 'include'
			});
			if (response.ok) {
				const data = await response.json();
				branches = data.branches ?? [];
				isGitRepo = branches.length > 0;
				// Set default to the default branch (main/master) or current branch
				const defaultBranch = branches.find(b => b.name === 'main' || b.name === 'master') ?? branches.find(b => b.is_current);
				if (defaultBranch) {
					baseBranch = defaultBranch.name;
				}
				// If it's a git repo, default to GitHub mode; otherwise, force local mode
				if (isGitRepo) {
					workMode = 'github';
				} else {
					workMode = 'local';
				}
			} else {
				// If branches endpoint fails, assume not a git repo
				isGitRepo = false;
				workMode = 'local';
			}
		} catch (err) {
			console.error('Failed to fetch branches:', err);
			isGitRepo = false;
			workMode = 'local';
		} finally {
			loadingBranches = false;
		}
	}

	// Watch for project selection changes to fetch branches
	$effect(() => {
		if (projectId) {
			fetchBranches(projectId);
		} else {
			branches = [];
			baseBranch = undefined;
			isGitRepo = false;
		}
	});

	async function handleLaunch() {
		if (!canLaunch) return;

		isLaunching = true;
		launchError = null;

		try {
			const useGitFeatures = workMode === 'github' && isGitRepo;
			const launchedAgent = await agents.launchAgent({
				name: name.trim(),
				prompt: prompt.trim(),
				profileId,
				projectId,
				autoBranch: useGitFeatures,    // Create branch only in GitHub mode
				autoPr: useGitFeatures,        // Create PR only in GitHub mode
				autoReview: false,             // No auto-review
				maxDurationMinutes: 0,         // Unlimited duration (run until complete)
				baseBranch: useGitFeatures ? baseBranch : undefined
			});

			// Transition to monitor mode
			actualAgentId = launchedAgent.id;
			mode = 'monitor';

			// Update the card's data to reflect the new agent
			deck.setCardDataId(card.id, launchedAgent.id);
			deck.setCardMeta(card.id, { agentId: launchedAgent.id });
			deck.setCardTitle(card.id, launchedAgent.name);

		} catch (err) {
			launchError = err instanceof Error ? err.message : 'Failed to launch agent';
			isLaunching = false;
		}
	}

	// ============================================
	// MONITOR MODE STATE
	// ============================================
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
		if (!actualAgentId) return;

		loading = true;
		error = null;
		try {
			const fetchedAgent = await agents.fetchAgent(actualAgentId);
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
				await agents.fetchLogs(actualAgentId, { limit: 10 });
				// Update agent with logs
				agent = agents.getAgent(actualAgentId) || agent;
			} else {
				error = 'Agent not found';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load agent';
		} finally {
			loading = false;
		}
	}

	// Subscribe to agent updates via store when in monitor mode
	$effect(() => {
		if (mode === 'monitor' && actualAgentId) {
			fetchAgent();
			agents.subscribeToAgent(actualAgentId);
		}
	});

	// Poll for updates every 5 seconds as backup (only in monitor mode)
	let pollInterval: ReturnType<typeof setInterval> | null = null;

	$effect(() => {
		if (pollInterval) {
			clearInterval(pollInterval);
			pollInterval = null;
		}

		if (mode === 'monitor' && actualAgentId) {
			pollInterval = setInterval(() => {
				const updated = agents.getAgent(actualAgentId!);
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
		}

		return () => {
			if (pollInterval) {
				clearInterval(pollInterval);
				pollInterval = null;
			}
		};
	});

	// Initialize launcher data on mount
	onMount(async () => {
		if (mode === 'launcher') {
			await Promise.all([fetchProfiles(), fetchProjects()]);
		}
	});

	onDestroy(() => {
		if (actualAgentId) {
			agents.unsubscribeFromAgent(actualAgentId);
		}
		if (durationInterval) {
			clearInterval(durationInterval);
		}
		if (pollInterval) {
			clearInterval(pollInterval);
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

	// Control actions
	async function handlePause() {
		if (!agent || !actualAgentId) return;
		try {
			await agents.pauseAgent(actualAgentId);
			agent = agents.getAgent(actualAgentId) || agent;
		} catch (e) {
			console.error('Failed to pause agent:', e);
		}
	}

	async function handleResume() {
		if (!agent || !actualAgentId) return;
		try {
			await agents.resumeAgent(actualAgentId);
			agent = agents.getAgent(actualAgentId) || agent;
		} catch (e) {
			console.error('Failed to resume agent:', e);
		}
	}

	async function handleCancel() {
		if (!agent || !actualAgentId) return;
		try {
			await agents.cancelAgent(actualAgentId);
			agent = agents.getAgent(actualAgentId) || agent;
		} catch (e) {
			console.error('Failed to cancel agent:', e);
		}
	}

	async function handleDelete() {
		if (!agent || !actualAgentId) return;
		try {
			await agents.deleteAgent(actualAgentId);
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

<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
	{#if mode === 'launcher'}
		<!-- LAUNCHER MODE UI -->
		<div class="launcher-content">
			<!-- Header -->
			<div class="launcher-header">
				<div class="launcher-header-icon">
					<Rocket class="w-5 h-5 text-primary" />
				</div>
				<div>
					<h3 class="launcher-title">Launch Agent</h3>
					<p class="launcher-subtitle">Start an autonomous coding agent</p>
				</div>
			</div>

			<!-- Content -->
			<div class="launcher-form">
				<!-- Error message -->
				{#if launchError}
					<div class="error-message">
						{launchError}
					</div>
				{/if}

				<!-- Task name -->
				<div class="form-group">
					<label for="agent-name" class="form-label">Task Name</label>
					<input
						id="agent-name"
						type="text"
						bind:value={name}
						placeholder="e.g., Implement user authentication"
						class="form-input"
						disabled={isLaunching}
					/>
				</div>

				<!-- Prompt -->
				<div class="form-group">
					<label for="agent-prompt" class="form-label">Instructions</label>
					<textarea
						id="agent-prompt"
						bind:value={prompt}
						placeholder="Describe what you want the agent to accomplish..."
						rows="5"
						class="form-textarea"
						disabled={isLaunching}
					></textarea>
					<p class="form-hint">Be specific about requirements, constraints, and expected outcomes</p>
				</div>

				<!-- Profile selector -->
				<div class="form-group">
					<label class="form-label with-icon">
						<User class="w-4 h-4 text-muted-foreground" />
						Profile
					</label>
					<select
						bind:value={profileId}
						disabled={isLaunching || loadingProfiles}
						class="form-select"
					>
						<option value={undefined}>
							{loadingProfiles ? 'Loading profiles...' : 'Use default'}
						</option>
						{#each profiles as profile}
							<option value={profile.id}>{profile.name}</option>
						{/each}
					</select>
				</div>

				<!-- Project selector -->
				<div class="form-group">
					<label class="form-label with-icon">
						<FolderKanban class="w-4 h-4 text-muted-foreground" />
						Project
					</label>
					<select
						bind:value={projectId}
						disabled={isLaunching || loadingProjects}
						class="form-select"
					>
						<option value={undefined}>
							{loadingProjects ? 'Loading projects...' : 'Select a project'}
						</option>
						{#each projects as project}
							<option value={project.id}>{project.name}</option>
						{/each}
					</select>
				</div>

				<!-- Work Mode selector (shows when project is selected) -->
				{#if projectId}
					<div class="form-group">
						<label class="form-label">Work Mode</label>
						<div class="work-mode-buttons">
							<!-- GitHub mode button -->
							<button
								type="button"
								onclick={() => workMode = 'github'}
								disabled={isLaunching || !isGitRepo}
								class="work-mode-btn {workMode === 'github' ? 'active' : ''} {!isGitRepo ? 'disabled' : ''}"
							>
								<GitBranch class="w-4 h-4" />
								<span>GitHub</span>
							</button>
							<!-- Local mode button -->
							<button
								type="button"
								onclick={() => workMode = 'local'}
								disabled={isLaunching}
								class="work-mode-btn {workMode === 'local' ? 'active' : ''}"
							>
								<Folder class="w-4 h-4" />
								<span>Local</span>
							</button>
						</div>
						{#if !isGitRepo && !loadingBranches}
							<p class="form-hint warning">This project is not a Git repository. Working in local mode.</p>
						{/if}
					</div>

					<!-- Branch selector (shows only in GitHub mode with branches) -->
					{#if workMode === 'github' && isGitRepo && branches.length > 0}
						<div class="form-group">
							<label class="form-label with-icon">
								<GitBranch class="w-4 h-4 text-muted-foreground" />
								Start From Branch
							</label>
							<select
								bind:value={baseBranch}
								disabled={isLaunching || loadingBranches}
								class="form-select"
							>
								{#if loadingBranches}
									<option value={undefined}>Loading branches...</option>
								{:else}
									{#each branches as branch}
										<option value={branch.name}>
											{branch.name}{branch.is_current ? ' (current)' : ''}
										</option>
									{/each}
								{/if}
							</select>
						</div>
					{/if}
				{/if}

				<!-- Workflow info -->
				<div class="workflow-info">
					<p class="workflow-title">Workflow</p>
					{#if workMode === 'github' && isGitRepo}
						<ul class="workflow-list">
							<li>• Agent creates a new feature branch</li>
							<li>• Works until the task is complete</li>
							<li>• Automatically creates a pull request</li>
							<li>• You can stop the agent at any time</li>
						</ul>
					{:else}
						<ul class="workflow-list">
							<li>• Agent works directly on the project folder</li>
							<li>• Changes are made without Git branching</li>
							<li>• Works until the task is complete</li>
							<li>• You can stop the agent at any time</li>
						</ul>
					{/if}
				</div>
			</div>

			<!-- Footer -->
			<div class="launcher-footer">
				<p class="launcher-hint">
					{#if canLaunch}
						Press <kbd class="kbd">Cmd+Enter</kbd> to launch
					{:else if isLaunching}
						Launching agent...
					{:else}
						Enter task name and instructions
					{/if}
				</p>
				<button
					onclick={handleLaunch}
					disabled={!canLaunch}
					class="launch-button"
				>
					{#if isLaunching}
						<Loader2 class="w-4 h-4 animate-spin" />
						Launching...
					{:else}
						<Rocket class="w-4 h-4" />
						Launch Agent
					{/if}
				</button>
			</div>
		</div>
	{:else}
		<!-- MONITOR MODE UI -->
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
	{/if}
</BaseCard>

<style>
	/* ============================================
	   LAUNCHER MODE STYLES
	   ============================================ */
	.launcher-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
		background: var(--card);
	}

	.launcher-header {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 16px;
		border-bottom: 1px solid var(--border);
		background: linear-gradient(to right, color-mix(in oklch, var(--primary) 8%, transparent), transparent);
		flex-shrink: 0;
	}

	.launcher-header-icon {
		width: 40px;
		height: 40px;
		border-radius: 10px;
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		box-shadow: var(--shadow-s);
	}

	.launcher-title {
		font-size: 1rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.launcher-subtitle {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0;
	}

	.launcher-form {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.error-message {
		padding: 10px 14px;
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		border-radius: 10px;
		font-size: 0.8125rem;
		color: var(--destructive);
		box-shadow: var(--shadow-s);
	}

	.form-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.form-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.form-label.with-icon {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.form-input,
	.form-textarea,
	.form-select {
		width: 100%;
		padding: 10px 14px;
		background: oklch(0.12 0.008 260);
		border: 1px solid var(--border);
		border-radius: 10px;
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 0.15s ease, box-shadow 0.15s ease, background-color 0.15s ease;
	}

	.form-input::placeholder,
	.form-textarea::placeholder {
		color: var(--muted-foreground);
	}

	.form-input:hover,
	.form-textarea:hover,
	.form-select:hover {
		border-color: color-mix(in oklch, var(--border) 100%, var(--primary) 30%);
	}

	.form-input:focus,
	.form-textarea:focus,
	.form-select:focus {
		outline: none;
		border-color: var(--primary);
		background: oklch(0.14 0.01 260);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	/* Select dropdown styling for dark mode */
	.form-select {
		cursor: pointer;
		appearance: none;
		-webkit-appearance: none;
		-moz-appearance: none;
		background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%239ca3af' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.5' d='M6 8l4 4 4-4'/%3e%3c/svg%3e");
		background-position: right 10px center;
		background-repeat: no-repeat;
		background-size: 1.25em 1.25em;
		padding-right: 2.5rem;
	}

	.form-select option {
		background: oklch(0.18 0.01 260);
		color: var(--foreground);
		padding: 8px;
	}

	.form-textarea {
		resize: none;
	}

	.form-hint {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		margin: 0;
	}

	.options-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		background: none;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		padding: 6px 0;
		transition: color 0.15s ease;
	}

	.options-toggle:hover {
		color: var(--foreground);
	}

	.options-chevron {
		width: 16px;
		height: 16px;
		transition: transform 0.15s ease;
	}

	.options-chevron.rotate-90 {
		transform: rotate(90deg);
	}

	.options-panel {
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 12px;
		padding-left: 16px;
		margin-left: 4px;
		border-left: 2px solid var(--border);
		background: color-mix(in oklch, var(--muted) 30%, transparent);
		border-radius: 0 8px 8px 0;
	}

	.options-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.options-section-label {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin: 0;
	}

	.toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		cursor: pointer;
		padding: 6px 8px;
		border-radius: 8px;
		transition: background-color 0.15s ease;
	}

	.toggle-row:hover {
		background: color-mix(in oklch, var(--accent) 50%, transparent);
	}

	.toggle-label {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.8125rem;
		color: var(--foreground);
	}

	.toggle-button {
		position: relative;
		width: 40px;
		height: 24px;
		border-radius: 12px;
		background: var(--muted);
		border: 1px solid var(--border);
		cursor: pointer;
		transition: background 0.15s ease, border-color 0.15s ease;
	}

	.toggle-button.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.toggle-knob {
		position: absolute;
		top: 3px;
		left: 3px;
		width: 16px;
		height: 16px;
		border-radius: 8px;
		background: var(--foreground);
		transition: transform 0.15s ease;
		box-shadow: var(--shadow-s);
	}

	.toggle-knob.active {
		transform: translateX(16px);
		background: var(--primary-foreground);
	}

	.launcher-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 14px 16px;
		border-top: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		flex-shrink: 0;
	}

	.launcher-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0;
	}

	.kbd {
		padding: 3px 7px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 5px;
		font-size: 0.6875rem;
		font-family: monospace;
		color: var(--foreground);
	}

	.launch-button {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 18px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: 10px;
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		transition: all 0.15s ease;
		box-shadow: var(--shadow-m);
	}

	.launch-button:hover:not(:disabled) {
		filter: brightness(1.1);
		box-shadow: var(--shadow-l);
		transform: translateY(-1px);
	}

	.launch-button:active:not(:disabled) {
		transform: translateY(0);
	}

	.launch-button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Workflow Info */
	.workflow-info {
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		border-radius: 10px;
		padding: 12px 14px;
		border: 1px solid var(--border);
	}

	.workflow-title {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0 0 8px 0;
	}

	.workflow-list {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.workflow-list li {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	/* Work Mode Buttons */
	.work-mode-buttons {
		display: flex;
		gap: 8px;
	}

	.work-mode-btn {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 10px 14px;
		background: oklch(0.12 0.008 260);
		border: 1px solid var(--border);
		border-radius: 10px;
		color: var(--muted-foreground);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.work-mode-btn:hover:not(:disabled) {
		background: color-mix(in oklch, var(--muted) 80%, transparent);
		border-color: color-mix(in oklch, var(--border) 100%, var(--primary) 20%);
	}

	.work-mode-btn.active {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: var(--primary);
		color: var(--primary);
	}

	.work-mode-btn.disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.form-hint.warning {
		color: oklch(0.7 0.15 80);
	}

	/* ============================================
	   MONITOR MODE STYLES
	   ============================================ */
	.agent-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
		background: var(--card);
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
