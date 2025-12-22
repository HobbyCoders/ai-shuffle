<script lang="ts">
	/**
	 * AgentLauncher - Modal for launching new agents
	 *
	 * Features:
	 * - Task name input
	 * - Large prompt textarea
	 * - Options for auto-branch, auto-PR, auto-review, max duration
	 * - Profile and project selectors (fetched from API)
	 */
	import { onMount } from 'svelte';
	import { X, Rocket, GitBranch, GitPullRequest, Clock, User, FolderKanban, Eye, Loader2 } from 'lucide-svelte';

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
	}

	interface Profile {
		id: string;
		name: string;
	}

	interface Project {
		id: string;
		name: string;
	}

	let { onClose, onLaunch }: Props = $props();

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
	let error = $state<string | null>(null);

	// Data from API
	let profiles = $state<Profile[]>([]);
	let projects = $state<Project[]>([]);
	let loadingProfiles = $state(true);
	let loadingProjects = $state(true);

	const durations = [
		{ value: 15, label: '15 minutes' },
		{ value: 30, label: '30 minutes' },
		{ value: 60, label: '1 hour' },
		{ value: 120, label: '2 hours' }
	];

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

	async function handleSubmit() {
		if (!canLaunch) return;

		isLaunching = true;
		error = null;

		try {
			await onLaunch({
				name: name.trim(),
				prompt: prompt.trim(),
				autoBranch,
				autoPR,
				autoReview,
				maxDuration,
				profileId,
				projectId
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

			<!-- Options toggle -->
			<button
				onclick={() => showOptions = !showOptions}
				class="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
			>
				<svg
					class="w-4 h-4 transition-transform {showOptions ? 'rotate-90' : ''}"
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
				<div class="space-y-4 pl-2 border-l-2 border-border">
					<!-- Git options -->
					<div class="space-y-3">
						<h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wider">Git</h4>

						<!-- Auto-branch toggle -->
						<label class="flex items-center justify-between cursor-pointer">
							<div class="flex items-center gap-2">
								<GitBranch class="w-4 h-4 text-muted-foreground" />
								<span class="text-sm text-foreground">Create branch automatically</span>
							</div>
							<button
								type="button"
								onclick={() => autoBranch = !autoBranch}
								disabled={isLaunching}
								class="relative w-10 h-6 rounded-full transition-colors {autoBranch ? 'bg-primary' : 'bg-muted'}"
							>
								<span
									class="absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform {autoBranch ? 'translate-x-4' : ''}"
								></span>
							</button>
						</label>

						<!-- Auto-PR toggle -->
						<label class="flex items-center justify-between cursor-pointer">
							<div class="flex items-center gap-2">
								<GitPullRequest class="w-4 h-4 text-muted-foreground" />
								<span class="text-sm text-foreground">Create PR on completion</span>
							</div>
							<button
								type="button"
								onclick={() => autoPR = !autoPR}
								disabled={isLaunching}
								class="relative w-10 h-6 rounded-full transition-colors {autoPR ? 'bg-primary' : 'bg-muted'}"
							>
								<span
									class="absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform {autoPR ? 'translate-x-4' : ''}"
								></span>
							</button>
						</label>

						<!-- Auto-Review toggle -->
						<label class="flex items-center justify-between cursor-pointer">
							<div class="flex items-center gap-2">
								<Eye class="w-4 h-4 text-muted-foreground" />
								<span class="text-sm text-foreground">Auto-review changes</span>
							</div>
							<button
								type="button"
								onclick={() => autoReview = !autoReview}
								disabled={isLaunching}
								class="relative w-10 h-6 rounded-full transition-colors {autoReview ? 'bg-primary' : 'bg-muted'}"
							>
								<span
									class="absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform {autoReview ? 'translate-x-4' : ''}"
								></span>
							</button>
						</label>
					</div>

					<!-- Duration -->
					<div>
						<label class="flex items-center gap-2 text-sm text-foreground mb-2">
							<Clock class="w-4 h-4 text-muted-foreground" />
							Max Duration
						</label>
						<select
							bind:value={maxDuration}
							disabled={isLaunching}
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						>
							{#each durations as duration}
								<option value={duration.value}>{duration.label}</option>
							{/each}
						</select>
					</div>

					<!-- Profile selector -->
					<div>
						<label class="flex items-center gap-2 text-sm text-foreground mb-2">
							<User class="w-4 h-4 text-muted-foreground" />
							Profile (optional)
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
							Project (optional)
						</label>
						<select
							bind:value={projectId}
							disabled={isLaunching || loadingProjects}
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						>
							<option value={undefined}>
								{loadingProjects ? 'Loading projects...' : 'Current workspace'}
							</option>
							{#each projects as project}
								<option value={project.id}>{project.name}</option>
							{/each}
						</select>
					</div>
				</div>
			{/if}
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
