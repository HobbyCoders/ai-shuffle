<script lang="ts">
	/**
	 * AgentLauncher - Modal/panel for launching a new background agent
	 *
	 * Features:
	 * - Task name input
	 * - Large prompt/instructions textarea
	 * - Options: auto-create branch, auto-PR, GitHub integration, max duration
	 * - Profile selector
	 * - Project/workspace selector
	 * - Launch button
	 */
	import { createEventDispatcher } from 'svelte';

	interface Profile {
		id: string;
		name: string;
		description?: string;
	}

	interface Project {
		id: string;
		name: string;
		path: string;
	}

	interface Props {
		open?: boolean;
		profiles?: Profile[];
		projects?: Project[];
		defaultProfileId?: string;
		defaultProjectId?: string;
		onlaunch?: (config: LaunchConfig) => void;
		oncancel?: () => void;
	}

	export interface LaunchConfig {
		name: string;
		prompt: string;
		profileId?: string;
		projectId?: string;
		autoCreateBranch: boolean;
		branchName?: string;
		autoPR: boolean;
		githubIntegration: boolean;
		maxDuration: string;
	}

	let {
		open = false,
		profiles = [],
		projects = [],
		defaultProfileId = '',
		defaultProjectId = '',
		onlaunch,
		oncancel
	}: Props = $props();

	// Form state
	let name = $state('');
	let prompt = $state('');
	let selectedProfileId = $state(defaultProfileId);
	let selectedProjectId = $state(defaultProjectId);
	let autoCreateBranch = $state(true);
	let branchName = $state('');
	let autoPR = $state(true);
	let githubIntegration = $state(true);
	let maxDuration = $state('4h');
	let showAdvanced = $state(false);

	// Validation
	const isValid = $derived(name.trim().length > 0 && prompt.trim().length > 0);

	// Generate branch name from task name
	function generateBranchName(taskName: string): string {
		return 'agent/' + taskName
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, '-')
			.replace(/^-|-$/g, '')
			.slice(0, 50);
	}

	// Auto-generate branch name when name changes
	$effect(() => {
		if (autoCreateBranch && name && !branchName) {
			branchName = generateBranchName(name);
		}
	});

	// Reset form
	function resetForm() {
		name = '';
		prompt = '';
		selectedProfileId = defaultProfileId;
		selectedProjectId = defaultProjectId;
		autoCreateBranch = true;
		branchName = '';
		autoPR = true;
		githubIntegration = true;
		maxDuration = '4h';
		showAdvanced = false;
	}

	// Handle launch
	function handleLaunch() {
		if (!isValid) return;

		const config: LaunchConfig = {
			name: name.trim(),
			prompt: prompt.trim(),
			profileId: selectedProfileId || undefined,
			projectId: selectedProjectId || undefined,
			autoCreateBranch,
			branchName: autoCreateBranch ? branchName : undefined,
			autoPR,
			githubIntegration,
			maxDuration
		};

		onlaunch?.(config);
		resetForm();
	}

	// Handle cancel
	function handleCancel() {
		resetForm();
		oncancel?.();
	}

	// Handle escape key
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			handleCancel();
		}
	}

	// Duration options
	const durationOptions = [
		{ value: '1h', label: '1 hour' },
		{ value: '4h', label: '4 hours' },
		{ value: '8h', label: '8 hours' },
		{ value: '24h', label: '24 hours' },
		{ value: 'unlimited', label: 'Unlimited' }
	];
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<!-- Backdrop -->
	<div class="fixed inset-0 z-50 flex items-center justify-center p-4">
		<button
			type="button"
			class="absolute inset-0 bg-black/60 backdrop-blur-sm"
			onclick={handleCancel}
			aria-label="Close"
		></button>

		<!-- Panel -->
		<div
			class="relative w-full max-w-2xl max-h-[90vh] bg-card border border-border rounded-2xl shadow-2xl flex flex-col overflow-hidden animate-modal-in"
			role="dialog"
			aria-modal="true"
			aria-labelledby="launcher-title"
		>
			<!-- Header -->
			<header class="shrink-0 px-6 py-4 border-b border-border bg-gradient-to-r from-card to-muted/30">
				<div class="flex items-center justify-between">
					<div class="flex items-center gap-3">
						<div class="w-10 h-10 rounded-xl bg-cyan-500/15 flex items-center justify-center">
							<svg class="w-5 h-5 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
							</svg>
						</div>
						<div>
							<h2 id="launcher-title" class="text-lg font-semibold text-foreground">Launch Agent</h2>
							<p class="text-xs text-muted-foreground">Start a new background task</p>
						</div>
					</div>
					<button
						type="button"
						onclick={handleCancel}
						class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
						aria-label="Close"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>
			</header>

			<!-- Body -->
			<div class="flex-1 overflow-y-auto p-6 space-y-6">
				<!-- Task name -->
				<div>
					<label for="task-name" class="block text-sm font-medium text-foreground mb-2">
						Task Name <span class="text-red-400">*</span>
					</label>
					<input
						id="task-name"
						type="text"
						bind:value={name}
						placeholder="e.g., Implement user authentication"
						class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-foreground placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
					/>
				</div>

				<!-- Instructions/Prompt -->
				<div>
					<label for="task-prompt" class="block text-sm font-medium text-foreground mb-2">
						Instructions <span class="text-red-400">*</span>
					</label>
					<textarea
						id="task-prompt"
						bind:value={prompt}
						placeholder="Describe in detail what the agent should accomplish. Include acceptance criteria, constraints, and any relevant context..."
						rows="8"
						class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-foreground placeholder-muted-foreground resize-y focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
					></textarea>
					<p class="text-xs text-muted-foreground mt-2">
						Be specific about what you want. The agent will work autonomously based on these instructions.
					</p>
				</div>

				<!-- Selectors row -->
				<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
					<!-- Profile selector -->
					<div>
						<label for="profile-select" class="block text-sm font-medium text-foreground mb-2">
							AI Profile
						</label>
						<select
							id="profile-select"
							bind:value={selectedProfileId}
							class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
						>
							<option value="">Default</option>
							{#each profiles as profile}
								<option value={profile.id}>{profile.name}</option>
							{/each}
						</select>
					</div>

					<!-- Project selector -->
					<div>
						<label for="project-select" class="block text-sm font-medium text-foreground mb-2">
							Project
						</label>
						<select
							id="project-select"
							bind:value={selectedProjectId}
							class="w-full bg-muted border border-border rounded-xl px-4 py-3 text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
						>
							<option value="">Current workspace</option>
							{#each projects as project}
								<option value={project.id}>{project.name}</option>
							{/each}
						</select>
					</div>
				</div>

				<!-- Git/GitHub options -->
				<div class="border border-border rounded-xl p-4 space-y-4">
					<div class="flex items-center justify-between">
						<h3 class="text-sm font-medium text-foreground">Git & GitHub</h3>
						<button
							type="button"
							onclick={() => githubIntegration = !githubIntegration}
							class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors {githubIntegration ? 'bg-primary' : 'bg-muted'}"
							role="switch"
							aria-checked={githubIntegration}
							aria-label="Toggle GitHub integration"
						>
							<span
								class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform {githubIntegration ? 'translate-x-6' : 'translate-x-1'}"
							></span>
						</button>
					</div>

					{#if githubIntegration}
						<div class="space-y-4 pt-2">
							<!-- Auto-create branch -->
							<label class="flex items-start gap-3 cursor-pointer group">
								<input
									type="checkbox"
									bind:checked={autoCreateBranch}
									class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
								/>
								<div class="flex-1">
									<span class="text-sm text-foreground group-hover:text-primary transition-colors">
										Auto-create branch
									</span>
									<p class="text-xs text-muted-foreground">Create a new branch for this agent's work</p>
								</div>
							</label>

							{#if autoCreateBranch}
								<div class="ml-7">
									<input
										type="text"
										bind:value={branchName}
										placeholder="agent/feature-name"
										class="w-full bg-card border border-border rounded-lg px-3 py-2 text-sm text-foreground font-mono placeholder-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
									/>
								</div>
							{/if}

							<!-- Auto-PR -->
							<label class="flex items-start gap-3 cursor-pointer group">
								<input
									type="checkbox"
									bind:checked={autoPR}
									class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
								/>
								<div class="flex-1">
									<span class="text-sm text-foreground group-hover:text-primary transition-colors">
										Auto-create PR on completion
									</span>
									<p class="text-xs text-muted-foreground">Open a pull request when the agent finishes</p>
								</div>
							</label>
						</div>
					{/if}
				</div>

				<!-- Advanced options -->
				<div>
					<button
						type="button"
						onclick={() => showAdvanced = !showAdvanced}
						class="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
					>
						<svg
							class="w-4 h-4 transition-transform {showAdvanced ? 'rotate-90' : ''}"
							fill="none"
							stroke="currentColor"
							viewBox="0 0 24 24"
						>
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
						</svg>
						Advanced options
					</button>

					{#if showAdvanced}
						<div class="mt-4 space-y-4 pl-6 border-l-2 border-border">
							<!-- Max duration -->
							<div>
								<label for="max-duration" class="block text-sm font-medium text-foreground mb-2">
									Max Duration
								</label>
								<select
									id="max-duration"
									bind:value={maxDuration}
									class="w-full max-w-xs bg-muted border border-border rounded-xl px-4 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
								>
									{#each durationOptions as option}
										<option value={option.value}>{option.label}</option>
									{/each}
								</select>
								<p class="text-xs text-muted-foreground mt-1">
									Agent will stop after this duration if not completed
								</p>
							</div>
						</div>
					{/if}
				</div>
			</div>

			<!-- Footer -->
			<footer class="shrink-0 px-6 py-4 border-t border-border bg-muted/30 flex flex-col-reverse sm:flex-row items-stretch sm:items-center justify-end gap-3">
				<button
					type="button"
					onclick={handleCancel}
					class="px-6 py-2.5 rounded-xl text-sm font-medium bg-muted text-foreground border border-border hover:bg-accent transition-colors"
				>
					Cancel
				</button>
				<button
					type="button"
					onclick={handleLaunch}
					disabled={!isValid}
					class="px-6 py-2.5 rounded-xl text-sm font-medium bg-gradient-to-r from-cyan-500 to-blue-500 text-white hover:from-cyan-400 hover:to-blue-400 transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
					</svg>
					Launch Agent
				</button>
			</footer>
		</div>
	</div>
{/if}

<style>
	@keyframes modal-in {
		from {
			opacity: 0;
			transform: scale(0.95) translateY(10px);
		}
		to {
			opacity: 1;
			transform: scale(1) translateY(0);
		}
	}

	.animate-modal-in {
		animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}
</style>
