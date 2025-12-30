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

	// Password visibility
	let showPassword = false;
	let showConfirmPassword = false;

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
						<div class="relative">
							<input
								type={showPassword ? "text" : "password"}
								id="password"
								bind:value={password}
								class="input pr-10"
								placeholder="Enter password"
								required
								minlength="8"
								autocomplete="new-password"
							/>
							<button
								type="button"
								class="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
								on:click={() => showPassword = !showPassword}
								tabindex="-1"
								aria-label={showPassword ? "Hide password" : "Show password"}
							>
								{#if showPassword}
									<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
										<path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
									</svg>
								{:else}
									<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
										<path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										<path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
									</svg>
								{/if}
							</button>
						</div>
					</div>

					<div>
						<label for="confirmPassword" class="block text-sm font-medium text-muted-foreground mb-1">
							Confirm Password
						</label>
						<div class="relative">
							<input
								type={showConfirmPassword ? "text" : "password"}
								id="confirmPassword"
								bind:value={confirmPassword}
								class="input pr-10"
								placeholder="Confirm password"
								required
								autocomplete="new-password"
							/>
							<button
								type="button"
								class="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
								on:click={() => showConfirmPassword = !showConfirmPassword}
								tabindex="-1"
								aria-label={showConfirmPassword ? "Hide password" : "Show password"}
							>
								{#if showConfirmPassword}
									<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
										<path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
									</svg>
								{:else}
									<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
										<path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										<path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
									</svg>
								{/if}
							</button>
						</div>
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
