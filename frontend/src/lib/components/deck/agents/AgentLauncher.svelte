<script lang="ts">
	/**
	 * AgentLauncher - Modal for launching new agents
	 *
	 * Features:
	 * - Task name input
	 * - Large prompt textarea
	 * - Options for auto-branch, auto-PR, max duration
	 * - Profile and project selectors
	 */
	import { X, Rocket, GitBranch, GitPullRequest, Clock, User, FolderKanban } from 'lucide-svelte';

	interface Props {
		onClose: () => void;
		onLaunch: (data: LaunchData) => void;
	}

	interface LaunchData {
		name: string;
		prompt: string;
		autoBranch: boolean;
		autoPR: boolean;
		maxDuration: number;
		profileId?: string;
		projectId?: string;
	}

	let { onClose, onLaunch }: Props = $props();

	// Form state
	let name = $state('');
	let prompt = $state('');
	let autoBranch = $state(true);
	let autoPR = $state(false);
	let maxDuration = $state(30); // minutes
	let profileId = $state<string | undefined>(undefined);
	let projectId = $state<string | undefined>(undefined);
	let showOptions = $state(false);

	// Mock profiles and projects
	const profiles = [
		{ id: 'default', name: 'Default' },
		{ id: 'backend', name: 'Backend Developer' },
		{ id: 'frontend', name: 'Frontend Developer' },
		{ id: 'fullstack', name: 'Full Stack' }
	];

	const projects = [
		{ id: 'current', name: 'Current Project' },
		{ id: 'api', name: 'API Service' },
		{ id: 'web', name: 'Web App' },
		{ id: 'mobile', name: 'Mobile App' }
	];

	const durations = [
		{ value: 15, label: '15 minutes' },
		{ value: 30, label: '30 minutes' },
		{ value: 60, label: '1 hour' },
		{ value: 120, label: '2 hours' }
	];

	// Validation
	const canLaunch = $derived(name.trim().length > 0 && prompt.trim().length > 0);

	function handleSubmit() {
		if (!canLaunch) return;

		onLaunch({
			name: name.trim(),
			prompt: prompt.trim(),
			autoBranch,
			autoPR,
			maxDuration,
			profileId,
			projectId
		});
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
	class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4"
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
								class="relative w-10 h-6 rounded-full transition-colors {autoPR ? 'bg-primary' : 'bg-muted'}"
							>
								<span
									class="absolute top-1 left-1 w-4 h-4 bg-white rounded-full transition-transform {autoPR ? 'translate-x-4' : ''}"
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
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						>
							<option value={undefined}>Use default</option>
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
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						>
							<option value={undefined}>Current workspace</option>
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
				{:else}
					Enter task name and instructions
				{/if}
			</p>
			<div class="flex items-center gap-2">
				<button
					onclick={onClose}
					class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
				>
					Cancel
				</button>
				<button
					onclick={handleSubmit}
					disabled={!canLaunch}
					class="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
				>
					<Rocket class="w-4 h-4" />
					Launch Agent
				</button>
			</div>
		</div>
	</div>
</div>
