<script lang="ts">
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import { auth, authLoading, authError } from '$lib/stores/auth';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig } from '$lib/api/auth';
	import type { WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';

	// Form state
	let username = '';
	let password = '';
	let confirmPassword = '';
	let localError = '';

	// Workspace configuration (for local mode)
	let workspaceConfig: WorkspaceConfig | null = null;
	let workspacePath = '';
	let workspaceValidation: WorkspaceValidation | null = null;
	let validatingWorkspace = false;
	let workspaceError = '';
	let loadingConfig = true;

	// Multi-step setup
	let currentStep = 1;
	$: totalSteps = workspaceConfig?.is_local_mode ? 2 : 1;
	$: isLocalMode = workspaceConfig?.is_local_mode ?? false;
	$: showWorkspaceStep = isLocalMode && currentStep === 1;

	onMount(async () => {
		try {
			workspaceConfig = await getWorkspaceConfig();
			if (workspaceConfig.is_local_mode) {
				workspacePath = workspaceConfig.default_path;
				// Validate the default path
				await validatePath();
			}
		} catch (e) {
			console.error('Failed to load workspace config:', e);
			// If we can't load config, assume not local mode and continue
		} finally {
			loadingConfig = false;
		}
	});

	async function validatePath() {
		if (!workspacePath.trim()) {
			workspaceValidation = null;
			workspaceError = '';
			return;
		}

		validatingWorkspace = true;
		workspaceError = '';

		try {
			workspaceValidation = await validateWorkspacePath(workspacePath);
			if (!workspaceValidation.valid && workspaceValidation.error) {
				workspaceError = workspaceValidation.error;
			}
		} catch (e: any) {
			workspaceError = e.detail || 'Failed to validate path';
			workspaceValidation = null;
		} finally {
			validatingWorkspace = false;
		}
	}

	// Debounce path validation
	let validateTimeout: ReturnType<typeof setTimeout>;
	function handlePathInput() {
		clearTimeout(validateTimeout);
		validateTimeout = setTimeout(validatePath, 500);
	}

	function nextStep() {
		if (currentStep === 1 && isLocalMode) {
			// Validate workspace before proceeding
			if (!workspaceValidation?.valid) {
				workspaceError = 'Please enter a valid workspace path';
				return;
			}
			currentStep = 2;
		}
	}

	function prevStep() {
		if (currentStep > 1) {
			currentStep--;
		}
	}

	async function handleSubmit() {
		localError = '';

		if (password !== confirmPassword) {
			localError = 'Passwords do not match';
			return;
		}

		if (password.length < 8) {
			localError = 'Password must be at least 8 characters';
			return;
		}

		try {
			// First create the admin account
			await auth.setup(username, password);

			// Then set workspace path if in local mode
			if (isLocalMode && workspacePath) {
				try {
					await setWorkspaceConfig(workspacePath);
				} catch (e: any) {
					console.error('Failed to set workspace:', e);
					// Don't block setup, workspace can be configured later
				}
			}

			goto('/');
		} catch (e) {
			// Error is handled by the store
		}
	}
</script>

<svelte:head>
	<title>Setup - AI Hub</title>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
</svelte:head>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card p-6 sm:p-8 w-full max-w-md">
		{#if loadingConfig}
			<!-- Loading state while fetching workspace config -->
			<div class="text-center py-8">
				<div class="text-4xl mb-4 animate-pulse">🚀</div>
				<p class="text-muted-foreground">Loading...</p>
			</div>
		{:else}
			<div class="text-center mb-6 sm:mb-8">
				<div class="text-4xl mb-3">🚀</div>
				<h1 class="text-xl sm:text-2xl font-bold text-foreground mb-2">Welcome to AI Hub</h1>
				{#if totalSteps > 1}
					<p class="text-muted-foreground text-sm sm:text-base">Step {currentStep} of {totalSteps}</p>
				{:else}
					<p class="text-muted-foreground text-sm sm:text-base">Create your admin account to get started</p>
				{/if}
			</div>

			{#if showWorkspaceStep}
				<!-- Step 1: Workspace Configuration (Local Mode Only) -->
				<div class="space-y-4">
					<div class="bg-primary/10 border border-primary/30 rounded-lg p-4 mb-4">
						<div class="flex items-start gap-3">
							<span class="text-primary text-xl">💻</span>
							<div>
								<h3 class="text-foreground font-medium mb-1">Local Mode Detected</h3>
								<p class="text-muted-foreground text-sm">
									Choose where to store your projects. This folder will contain all your code and files.
								</p>
							</div>
						</div>
					</div>

					<div>
						<label for="workspacePath" class="block text-sm font-medium text-muted-foreground mb-1">
							Workspace Folder
						</label>
						<input
							type="text"
							id="workspacePath"
							bind:value={workspacePath}
							on:input={handlePathInput}
							class="input font-mono text-sm"
							placeholder="Enter workspace path"
						/>
						{#if workspaceConfig?.default_path}
							<p class="text-muted-foreground text-xs mt-1">
								Default: {workspaceConfig.default_path}
							</p>
						{/if}
					</div>

					{#if validatingWorkspace}
						<div class="text-muted-foreground text-sm flex items-center gap-2">
							<span class="inline-block animate-spin">&#9696;</span>
							Validating path...
						</div>
					{:else if workspaceValidation}
						{#if workspaceValidation.valid}
							<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm flex items-center gap-2">
								<span>✓</span>
								<span>
									{workspaceValidation.exists ? 'Directory exists and is writable' : 'Directory will be created'}
								</span>
							</div>
						{:else}
							<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">
								{workspaceError || 'Invalid path'}
							</div>
						{/if}
					{/if}

					{#if workspaceError && !workspaceValidation}
						<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">
							{workspaceError}
						</div>
					{/if}

					<button
						type="button"
						on:click={nextStep}
						class="btn btn-primary w-full py-3"
						disabled={validatingWorkspace || !workspaceValidation?.valid}
					>
						Continue
					</button>
				</div>
			{:else}
				<!-- Step 2 (or Step 1 in Docker mode): Admin Account Setup -->
				<form on:submit|preventDefault={handleSubmit} class="space-y-4">
					{#if totalSteps > 1}
						<p class="text-muted-foreground text-sm mb-4">Create your admin account to get started</p>
					{/if}

					<div>
						<label for="username" class="block text-sm font-medium text-muted-foreground mb-1">
							Username
						</label>
						<input
							type="text"
							id="username"
							bind:value={username}
							class="input"
							placeholder="admin"
							required
							minlength="3"
							autocomplete="username"
						/>
					</div>

					<div>
						<label for="password" class="block text-sm font-medium text-muted-foreground mb-1">
							Password
						</label>
						<input
							type="password"
							id="password"
							bind:value={password}
							class="input"
							placeholder="Enter password"
							required
							minlength="8"
							autocomplete="new-password"
						/>
					</div>

					<div>
						<label for="confirmPassword" class="block text-sm font-medium text-muted-foreground mb-1">
							Confirm Password
						</label>
						<input
							type="password"
							id="confirmPassword"
							bind:value={confirmPassword}
							class="input"
							placeholder="Confirm password"
							required
							autocomplete="new-password"
						/>
					</div>

					{#if localError || $authError}
						<div class="bg-destructive/10 border border-destructive text-destructive px-3 sm:px-4 py-3 rounded-lg text-sm">
							{localError || $authError}
						</div>
					{/if}

					<div class="flex gap-3">
						{#if totalSteps > 1}
							<button
								type="button"
								on:click={prevStep}
								class="btn btn-secondary flex-1 py-3"
							>
								Back
							</button>
						{/if}
						<button
							type="submit"
							class="btn btn-primary flex-1 py-3"
							disabled={$authLoading}
						>
							{#if $authLoading}
								<span class="inline-block animate-spin mr-2">&#9696;</span>
							{/if}
							Create Account
						</button>
					</div>
				</form>
			{/if}
		{/if}
	</div>
</div>
