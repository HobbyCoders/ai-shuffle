<script lang="ts">
	/**
	 * AgentCard - Agent card for The Deck
	 *
	 * Operates in two modes:
	 * 1. Launcher Mode: When no agentId is provided, shows agent configuration UI
	 * 2. Monitor Mode: When agentId is provided, shows agent monitoring UI
	 *
	 * This keeps everything within the card-based paradigm - no modals needed.
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
		GitPullRequest,
		Clock,
		User,
		FolderKanban,
		Eye,
		X
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
	interface LaunchData {
		name: string;
		prompt: string;
		autoBranch: boolean;
		autoPR: boolean;
		autoReview: boolean;
		maxDuration: number;
		profileId?: string;
		projectId?: string;
		baseBranch?: string;
	}

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

	// Form state
	let name = $state('');
	let prompt = $state('');
	let autoBranch = $state(true);
	let autoPR = $state(false);
	let autoReview = $state(false);
	let maxDuration = $state(30); // minutes
	let profileId = $state<string | undefined>(undefined);
	let projectId = $state<string | undefined>(undefined);
	let showOptions = $state(false);
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

	const durations = [
		{ value: 0, label: 'Unlimited' },
		{ value: 15, label: '15 minutes' },
		{ value: 30, label: '30 minutes' },
		{ value: 60, label: '1 hour' },
		{ value: 120, label: '2 hours' }
	];

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
		try {
			const response = await fetch(`/api/v1/projects/${projId}/git/branches`, {
				credentials: 'include'
			});
			if (response.ok) {
				const data = await response.json();
				branches = data.branches ?? [];
				// Set default to the default branch (main/master) or current branch
				const defaultBranch = branches.find(b => b.name === 'main' || b.name === 'master') ?? branches.find(b => b.is_current);
				if (defaultBranch) {
					baseBranch = defaultBranch.name;
				}
			}
		} catch (err) {
			console.error('Failed to fetch branches:', err);
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
		}
	});

	async function handleLaunch() {
		if (!canLaunch) return;

		isLaunching = true;
		launchError = null;

		try {
			const launchedAgent = await agents.launchAgent({
				name: name.trim(),
				prompt: prompt.trim(),
				profileId,
				projectId,
				autoBranch,
				autoPr: autoPR,
				autoReview,
				maxDurationMinutes: maxDuration,
				baseBranch
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

<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
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

				<!-- Options toggle -->
				<button
					onclick={() => showOptions = !showOptions}
					class="options-toggle"
				>
					<svg
						class="options-chevron {showOptions ? 'rotate-90' : ''}"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
					</svg>
					Advanced Options
				</button>

				<!-- Options (collapsible) -->
				{#if showOptions}
					<div class="options-panel">
						<!-- Git options -->
						<div class="options-section">
							<h4 class="options-section-label">Git</h4>

							<!-- Auto-branch toggle -->
							<label class="toggle-row">
								<div class="toggle-label">
									<GitBranch class="w-4 h-4 text-muted-foreground" />
									<span>Create branch automatically</span>
								</div>
								<button
									type="button"
									onclick={() => autoBranch = !autoBranch}
									disabled={isLaunching}
									class="toggle-button {autoBranch ? 'active' : ''}"
								>
									<span class="toggle-knob {autoBranch ? 'active' : ''}"></span>
								</button>
							</label>

							<!-- Auto-PR toggle -->
							<label class="toggle-row">
								<div class="toggle-label">
									<GitPullRequest class="w-4 h-4 text-muted-foreground" />
									<span>Create PR on completion</span>
								</div>
								<button
									type="button"
									onclick={() => autoPR = !autoPR}
									disabled={isLaunching}
									class="toggle-button {autoPR ? 'active' : ''}"
								>
									<span class="toggle-knob {autoPR ? 'active' : ''}"></span>
								</button>
							</label>

							<!-- Auto-Review toggle -->
							<label class="toggle-row">
								<div class="toggle-label">
									<Eye class="w-4 h-4 text-muted-foreground" />
									<span>Auto-review changes</span>
								</div>
								<button
									type="button"
									onclick={() => autoReview = !autoReview}
									disabled={isLaunching}
									class="toggle-button {autoReview ? 'active' : ''}"
								>
									<span class="toggle-knob {autoReview ? 'active' : ''}"></span>
								</button>
							</label>
						</div>

						<!-- Duration -->
						<div class="form-group">
							<label class="form-label with-icon">
								<Clock class="w-4 h-4 text-muted-foreground" />
								Max Duration
							</label>
							<select
								bind:value={maxDuration}
								disabled={isLaunching}
								class="form-select"
							>
								{#each durations as d}
									<option value={d.value}>{d.label}</option>
								{/each}
							</select>
						</div>

						<!-- Profile selector -->
						<div class="form-group">
							<label class="form-label with-icon">
								<User class="w-4 h-4 text-muted-foreground" />
								Profile (optional)
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
								Project (optional)
							</label>
							<select
								bind:value={projectId}
								disabled={isLaunching || loadingProjects}
								class="form-select"
							>
								<option value={undefined}>
									{loadingProjects ? 'Loading projects...' : 'Current workspace'}
								</option>
								{#each projects as project}
									<option value={project.id}>{project.name}</option>
								{/each}
							</select>
						</div>

						<!-- Branch selector (shows when project is selected) -->
						{#if projectId && branches.length > 0}
							<div class="form-group">
								<label class="form-label with-icon">
									<GitBranch class="w-4 h-4 text-muted-foreground" />
									Base Branch
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
								<p class="form-hint">The worktree will be created from this branch</p>
							</div>
						{/if}
					</div>
				{/if}
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
	}

	.launcher-header {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 16px;
		border-bottom: 1px solid hsl(var(--border) / 0.5);
		flex-shrink: 0;
	}

	.launcher-header-icon {
		width: 40px;
		height: 40px;
		border-radius: 8px;
		background: hsl(var(--primary) / 0.1);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.launcher-title {
		font-size: 1rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin: 0;
	}

	.launcher-subtitle {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
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
		padding: 8px 12px;
		background: hsl(var(--destructive) / 0.1);
		border: 1px solid hsl(var(--destructive) / 0.2);
		border-radius: 8px;
		font-size: 0.8125rem;
		color: hsl(var(--destructive));
	}

	.form-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.form-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: hsl(var(--foreground));
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
		padding: 8px 12px;
		background: hsl(var(--background));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
	}

	.form-input::placeholder,
	.form-textarea::placeholder {
		color: hsl(var(--muted-foreground));
	}

	.form-input:focus,
	.form-textarea:focus,
	.form-select:focus {
		outline: none;
		border-color: hsl(var(--primary) / 0.5);
		box-shadow: 0 0 0 2px hsl(var(--primary) / 0.1);
	}

	.form-textarea {
		resize: none;
	}

	.form-hint {
		font-size: 0.6875rem;
		color: hsl(var(--muted-foreground));
		margin: 0;
	}

	.options-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		background: none;
		border: none;
		color: hsl(var(--muted-foreground));
		font-size: 0.8125rem;
		cursor: pointer;
		padding: 0;
		transition: color 0.15s ease;
	}

	.options-toggle:hover {
		color: hsl(var(--foreground));
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
		padding-left: 8px;
		border-left: 2px solid hsl(var(--border));
	}

	.options-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.options-section-label {
		font-size: 0.6875rem;
		font-weight: 500;
		color: hsl(var(--muted-foreground));
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin: 0;
	}

	.toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		cursor: pointer;
	}

	.toggle-label {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.8125rem;
		color: hsl(var(--foreground));
	}

	.toggle-button {
		position: relative;
		width: 40px;
		height: 24px;
		border-radius: 12px;
		background: hsl(var(--muted));
		border: none;
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.toggle-button.active {
		background: hsl(var(--primary));
	}

	.toggle-knob {
		position: absolute;
		top: 4px;
		left: 4px;
		width: 16px;
		height: 16px;
		border-radius: 8px;
		background: white;
		transition: transform 0.15s ease;
	}

	.toggle-knob.active {
		transform: translateX(16px);
	}

	.launcher-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		border-top: 1px solid hsl(var(--border) / 0.5);
		background: hsl(var(--muted) / 0.3);
		flex-shrink: 0;
	}

	.launcher-hint {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin: 0;
	}

	.kbd {
		padding: 2px 6px;
		background: hsl(var(--muted));
		border-radius: 4px;
		font-size: 0.6875rem;
		font-family: monospace;
	}

	.launch-button {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 16px;
		background: hsl(var(--primary));
		color: hsl(var(--primary-foreground));
		border: none;
		border-radius: 8px;
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.launch-button:hover:not(:disabled) {
		background: hsl(var(--primary) / 0.9);
	}

	.launch-button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* ============================================
	   MONITOR MODE STYLES
	   ============================================ */
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
