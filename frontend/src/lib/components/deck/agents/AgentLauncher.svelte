<script lang="ts">
	/**
	 * AgentLauncher - Modal for launching new agents
	 *
	 * Features:
	 * - Task name input
	 * - Large prompt textarea
	 * - Profile and project selectors (fetched from API)
	 * - Work mode: GitHub (branch + PR) or Local (direct changes)
	 * - Base branch selector (for GitHub mode)
	 *
	 * Workflow modes:
	 * - GitHub: Creates feature branch, commits changes, creates PR on completion
	 * - Local: Works directly on project folder, no git features
	 */
	import { onMount } from 'svelte';
	import { X, Rocket, GitBranch, User, FolderKanban, Loader2, Folder } from 'lucide-svelte';

	interface Props {
		onClose: () => void;
		onLaunch: (data: LaunchData) => void;
	}

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
		current: boolean;
	}

	interface Profile {
		id: string;
		name: string;
	}

	interface Project {
		id: string;
		name: string;
		is_git_repo?: boolean;
	}

	type WorkMode = 'github' | 'local';

	let { onClose, onLaunch }: Props = $props();

	// Form state
	let name = $state('');
	let prompt = $state('');
	let profileId = $state<string | undefined>(undefined);
	let projectId = $state<string | undefined>(undefined);
	let workMode = $state<WorkMode>('github');
	let isLaunching = $state(false);
	let error = $state<string | null>(null);

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

	// Fetch profiles and projects on mount
	onMount(async () => {
		await Promise.all([fetchProfiles(), fetchProjects()]);
	});

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
			const response = await fetch(`/api/v1/projects/${projId}/git/branches?include_remote=false`, {
				credentials: 'include'
			});
			if (response.ok) {
				const data = await response.json();
				branches = data.branches ?? [];
				isGitRepo = branches.length > 0;
				// Set default to the default branch (main/master) or current branch
				const defaultBranch = branches.find(b => b.name === 'main' || b.name === 'master') ?? branches.find(b => b.current);
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

	async function handleSubmit() {
		if (!canLaunch) return;

		isLaunching = true;
		error = null;

		try {
			const useGitFeatures = workMode === 'github' && isGitRepo;
			await onLaunch({
				name: name.trim(),
				prompt: prompt.trim(),
				autoBranch: useGitFeatures,    // Create branch only in GitHub mode
				autoPR: useGitFeatures,        // Create PR only in GitHub mode
				autoReview: false,             // No auto-review
				maxDuration: 0,                // Unlimited duration (run until complete)
				profileId,
				projectId,
				baseBranch: useGitFeatures ? baseBranch : undefined
			});
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to launch agent';
			isLaunching = false;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose();
		} else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && canLaunch) {
			handleSubmit();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Modal backdrop -->
<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
	class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4"
	onclick={onClose}
>
	<!-- Modal content -->
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="bg-card rounded-2xl w-full max-w-lg max-h-[90vh] overflow-hidden shadow-2xl border border-border flex flex-col"
		onclick={(e) => e.stopPropagation()}
	>
		<!-- Header -->
		<div class="flex items-center justify-between px-6 py-4 border-b border-border">
			<div class="flex items-center gap-3">
				<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center">
					<Rocket class="w-5 h-5 text-primary" />
				</div>
				<div>
					<h2 class="text-lg font-semibold text-foreground">Launch Agent</h2>
					<p class="text-xs text-muted-foreground">Start an autonomous coding agent</p>
				</div>
			</div>
			<button
				onclick={onClose}
				class="p-2 hover:bg-muted rounded-lg transition-colors"
			>
				<X class="w-5 h-5 text-muted-foreground" />
			</button>
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-y-auto p-6 space-y-4">
			<!-- Error message -->
			{#if error}
				<div class="px-3 py-2 bg-red-500/10 border border-red-500/20 rounded-lg text-sm text-red-500">
					{error}
				</div>
			{/if}

			<!-- Task name -->
			<div>
				<label for="agent-name" class="block text-sm font-medium text-foreground mb-1.5">
					Task Name
				</label>
				<input
					id="agent-name"
					type="text"
					bind:value={name}
					placeholder="e.g., Implement user authentication"
					class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					disabled={isLaunching}
				/>
			</div>

			<!-- Prompt -->
			<div>
				<label for="agent-prompt" class="block text-sm font-medium text-foreground mb-1.5">
					Instructions
				</label>
				<textarea
					id="agent-prompt"
					bind:value={prompt}
					placeholder="Describe what you want the agent to accomplish..."
					rows="6"
					class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
					disabled={isLaunching}
				></textarea>
				<p class="text-xs text-muted-foreground mt-1">
					Be specific about requirements, constraints, and expected outcomes
				</p>
			</div>

			<!-- Profile selector -->
			<div>
				<label class="flex items-center gap-2 text-sm text-foreground mb-2">
					<User class="w-4 h-4 text-muted-foreground" />
					Profile
				</label>
				<select
					bind:value={profileId}
					disabled={isLaunching || loadingProfiles}
					class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
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
			<div>
				<label class="flex items-center gap-2 text-sm text-foreground mb-2">
					<FolderKanban class="w-4 h-4 text-muted-foreground" />
					Project
				</label>
				<select
					bind:value={projectId}
					disabled={isLaunching || loadingProjects}
					class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
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
				<div>
					<label class="flex items-center gap-2 text-sm text-foreground mb-2">
						Work Mode
					</label>
					<div class="flex gap-2">
						<!-- GitHub mode button -->
						<button
							type="button"
							onclick={() => workMode = 'github'}
							disabled={isLaunching || !isGitRepo}
							class="flex-1 flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg border transition-all {workMode === 'github' ? 'bg-primary/10 border-primary text-primary' : 'bg-background border-border text-muted-foreground hover:bg-muted/50'} {!isGitRepo ? 'opacity-50 cursor-not-allowed' : ''}"
						>
							<GitBranch class="w-4 h-4" />
							<span class="text-sm font-medium">GitHub</span>
						</button>
						<!-- Local mode button -->
						<button
							type="button"
							onclick={() => workMode = 'local'}
							disabled={isLaunching}
							class="flex-1 flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg border transition-all {workMode === 'local' ? 'bg-primary/10 border-primary text-primary' : 'bg-background border-border text-muted-foreground hover:bg-muted/50'}"
						>
							<Folder class="w-4 h-4" />
							<span class="text-sm font-medium">Local</span>
						</button>
					</div>
					{#if !isGitRepo && !loadingBranches}
						<p class="text-xs text-amber-500 mt-1.5">
							This project is not a Git repository. Working in local mode.
						</p>
					{/if}
				</div>

				<!-- Branch selector (shows only in GitHub mode with branches) -->
				{#if workMode === 'github' && isGitRepo && branches.length > 0}
					<div>
						<label class="flex items-center gap-2 text-sm text-foreground mb-2">
							<GitBranch class="w-4 h-4 text-muted-foreground" />
							Start From Branch
						</label>
						<select
							bind:value={baseBranch}
							disabled={isLaunching || loadingBranches}
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						>
							{#if loadingBranches}
								<option value={undefined}>Loading branches...</option>
							{:else}
								{#each branches as branch}
									<option value={branch.name}>
										{branch.name}{branch.current ? ' (current)' : ''}
									</option>
								{/each}
							{/if}
						</select>
					</div>
				{/if}
			{/if}

			<!-- Workflow info -->
			<div class="bg-muted/50 rounded-lg p-3 text-xs text-muted-foreground">
				<p class="font-medium text-foreground mb-1">Workflow</p>
				{#if workMode === 'github' && isGitRepo}
					<ul class="space-y-1">
						<li>• Agent creates a new feature branch</li>
						<li>• Works until the task is complete</li>
						<li>• Automatically creates a pull request</li>
						<li>• You can stop the agent at any time</li>
					</ul>
				{:else}
					<ul class="space-y-1">
						<li>• Agent works directly on the project folder</li>
						<li>• Changes are made without Git branching</li>
						<li>• Works until the task is complete</li>
						<li>• You can stop the agent at any time</li>
					</ul>
				{/if}
			</div>
		</div>

		<!-- Footer -->
		<div class="flex items-center justify-between px-6 py-4 border-t border-border bg-muted/30">
			<p class="text-xs text-muted-foreground">
				{#if canLaunch}
					Press <kbd class="px-1.5 py-0.5 bg-muted rounded text-xs">Cmd+Enter</kbd> to launch
				{:else if isLaunching}
					Launching agent...
				{:else}
					Enter task name and instructions
				{/if}
			</p>
			<div class="flex items-center gap-2">
				<button
					onclick={onClose}
					disabled={isLaunching}
					class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
				>
					Cancel
				</button>
				<button
					onclick={handleSubmit}
					disabled={!canLaunch}
					class="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
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
	</div>
</div>
