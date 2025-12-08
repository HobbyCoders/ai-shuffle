<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, username, isAdmin, apiUser, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import { api } from '$lib/api/client';
	import type { ApiUser, ApiUserWithKey, Profile, WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import type { Project } from '$lib/stores/chat';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig } from '$lib/api/auth';

	// Tab management
	type SettingsTab = 'general' | 'authentication' | 'api-users' | 'integrations';
	let activeTab: SettingsTab = 'general';

	// Mobile sidebar state
	let sidebarOpen = false;

	function toggleSidebar() {
		sidebarOpen = !sidebarOpen;
	}

	function closeSidebar() {
		sidebarOpen = false;
	}

	function selectTab(tabId: SettingsTab) {
		activeTab = tabId;
		// Auto-close sidebar on mobile when selecting a tab
		sidebarOpen = false;
	}

	let apiUsers: ApiUser[] = [];
	let profiles: Profile[] = [];
	let projects: Project[] = [];
	let loading = true;
	let error = '';

	// Form state
	let showCreateForm = false;
	let editingUser: ApiUser | null = null;
	let newlyCreatedKey: string | null = null;
	let regeneratedKey: string | null = null;

	// Workspace state
	let workspaceConfig: WorkspaceConfig | null = null;
	let workspacePath = '';
	let workspaceValidation: WorkspaceValidation | null = null;
	let validatingWorkspace = false;
	let savingWorkspace = false;
	let workspaceError = '';
	let workspaceSuccess = '';

	// Auth state
	let githubToken = '';
	let githubLoginLoading = false;
	let claudeLoginLoading = false;
	let claudeOAuthUrl: string | null = null;
	let claudeAuthCode = '';
	let claudeCompletingLogin = false;
	let githubUser: string | null = null;

	// Integration settings
	let openaiApiKey = '';
	let openaiApiKeyMasked = '';
	let savingOpenaiKey = false;
	let openaiKeySuccess = '';
	let openaiKeyError = '';

	// Image generation (Nano Banana) settings
	interface ImageModel {
		id: string;
		name: string;
		description: string;
		price_per_image: number;
	}
	let imageModels: ImageModel[] = [];
	let imageProvider = '';
	let imageModel = '';
	let imageApiKey = '';
	let imageApiKeyMasked = '';
	let savingImageConfig = false;
	let imageConfigSuccess = '';
	let imageConfigError = '';
	let selectedImageModel = '';
	let testingImage = false;
	let testImageResult: { success: boolean; image_base64?: string; mime_type?: string; error?: string } | null = null;

	let formData = {
		name: '',
		description: '',
		project_id: '',
		profile_id: ''
	};

	onMount(async () => {
		// Redirect non-admin users - they shouldn't see this page
		if (!$isAdmin) {
			goto('/');
			return;
		}
		await loadData();
		await loadAuthStatus();
		await loadWorkspaceConfig();
		await loadIntegrationSettings();
	});

	async function loadData() {
		loading = true;
		error = '';
		try {
			const [usersRes, profilesRes, projectsRes] = await Promise.all([
				api.get<ApiUser[]>('/api-users'),
				api.get<Profile[]>('/profiles'),
				api.get<Project[]>('/projects')
			]);
			apiUsers = usersRes;
			profiles = profilesRes;
			projects = projectsRes;
		} catch (e: any) {
			error = e.detail || 'Failed to load data';
		}
		loading = false;
	}

	async function loadAuthStatus() {
		try {
			const ghStatus = await api.get<{authenticated: boolean, user: string | null}>('/auth/github/status');
			githubUser = ghStatus.user;
		} catch (e) {
			console.error('Failed to load GitHub status:', e);
		}
	}

	// Workspace Configuration
	async function loadWorkspaceConfig() {
		try {
			workspaceConfig = await getWorkspaceConfig();
			workspacePath = workspaceConfig.workspace_path;
		} catch (e) {
			console.error('Failed to load workspace config:', e);
		}
	}

	// Integration Settings
	async function loadIntegrationSettings() {
		try {
			const settings = await api.get<{
				openai_api_key_set: boolean;
				openai_api_key_masked: string;
				image_provider: string | null;
				image_model: string | null;
				image_api_key_set: boolean;
				image_api_key_masked: string;
			}>('/settings/integrations');
			openaiApiKeyMasked = settings.openai_api_key_masked;
			imageProvider = settings.image_provider || '';
			imageModel = settings.image_model || '';
			imageApiKeyMasked = settings.image_api_key_masked || '';
			selectedImageModel = settings.image_model || '';
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}

		// Load available image models
		try {
			const modelsResponse = await api.get<{models: ImageModel[]}>('/settings/integrations/image/models');
			imageModels = modelsResponse.models;
			if (!selectedImageModel && imageModels.length > 0) {
				selectedImageModel = imageModels[0].id;
			}
		} catch (e) {
			console.error('Failed to load image models:', e);
		}
	}

	async function saveOpenaiApiKey() {
		if (!openaiApiKey.trim()) {
			openaiKeyError = 'Please enter an API key';
			return;
		}

		savingOpenaiKey = true;
		openaiKeyError = '';
		openaiKeySuccess = '';

		try {
			const result = await api.post<{success: boolean, masked_key: string}>('/settings/integrations/openai', {
				api_key: openaiApiKey
			});
			openaiApiKeyMasked = result.masked_key;
			openaiApiKey = '';
			openaiKeySuccess = 'OpenAI API key saved successfully';
		} catch (e: any) {
			openaiKeyError = e.detail || 'Failed to save API key';
		} finally {
			savingOpenaiKey = false;
		}
	}

	async function removeOpenaiApiKey() {
		if (!confirm('Are you sure you want to remove the OpenAI API key?')) {
			return;
		}

		savingOpenaiKey = true;
		openaiKeyError = '';
		openaiKeySuccess = '';

		try {
			await api.delete('/settings/integrations/openai');
			openaiApiKeyMasked = '';
			openaiKeySuccess = 'OpenAI API key removed';
		} catch (e: any) {
			openaiKeyError = e.detail || 'Failed to remove API key';
		} finally {
			savingOpenaiKey = false;
		}
	}

	// Image Generation (Nano Banana) functions
	async function saveImageConfig() {
		if (!imageApiKey.trim() && !imageApiKeyMasked) {
			imageConfigError = 'Please enter an API key';
			return;
		}

		if (!selectedImageModel) {
			imageConfigError = 'Please select a model';
			return;
		}

		savingImageConfig = true;
		imageConfigError = '';
		imageConfigSuccess = '';

		try {
			const result = await api.post<{success: boolean, provider: string, model: string, masked_key: string}>('/settings/integrations/image', {
				provider: 'google',
				model: selectedImageModel,
				api_key: imageApiKey || imageApiKeyMasked  // Use existing key if not changed
			});
			imageProvider = result.provider;
			imageModel = result.model;
			imageApiKeyMasked = result.masked_key;
			imageApiKey = '';
			imageConfigSuccess = 'Image generation configured successfully';
			testImageResult = null;
		} catch (e: any) {
			imageConfigError = e.detail || 'Failed to save configuration';
		} finally {
			savingImageConfig = false;
		}
	}

	async function removeImageConfig() {
		if (!confirm('Are you sure you want to remove the image generation configuration?')) {
			return;
		}

		savingImageConfig = true;
		imageConfigError = '';
		imageConfigSuccess = '';

		try {
			await api.delete('/settings/integrations/image');
			imageProvider = '';
			imageModel = '';
			imageApiKeyMasked = '';
			imageConfigSuccess = 'Image generation configuration removed';
			testImageResult = null;
		} catch (e: any) {
			imageConfigError = e.detail || 'Failed to remove configuration';
		} finally {
			savingImageConfig = false;
		}
	}

	async function testImageGeneration() {
		testingImage = true;
		testImageResult = null;
		imageConfigError = '';

		try {
			const result = await api.post<{success: boolean, image_base64?: string, mime_type?: string, error?: string}>('/settings/generate-image', {
				prompt: 'A cute orange cat sitting on a windowsill, digital art style'
			});
			testImageResult = result;
			if (!result.success) {
				imageConfigError = result.error || 'Image generation failed';
			}
		} catch (e: any) {
			imageConfigError = e.detail || 'Failed to test image generation';
			testImageResult = { success: false, error: e.detail || 'Failed to test image generation' };
		} finally {
			testingImage = false;
		}
	}

	async function validateWorkspace() {
		if (!workspacePath.trim()) {
			workspaceValidation = null;
			workspaceError = '';
			return;
		}

		validatingWorkspace = true;
		workspaceError = '';
		workspaceSuccess = '';

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

	// Debounce workspace validation
	let workspaceValidateTimeout: ReturnType<typeof setTimeout>;
	function handleWorkspaceInput() {
		clearTimeout(workspaceValidateTimeout);
		workspaceSuccess = '';
		workspaceValidateTimeout = setTimeout(validateWorkspace, 500);
	}

	async function saveWorkspace() {
		if (!workspaceValidation?.valid) {
			workspaceError = 'Please enter a valid workspace path';
			return;
		}

		savingWorkspace = true;
		workspaceError = '';
		workspaceSuccess = '';

		try {
			workspaceConfig = await setWorkspaceConfig(workspacePath);
			workspaceSuccess = 'Workspace folder updated successfully';
		} catch (e: any) {
			workspaceError = e.detail || 'Failed to save workspace path';
		} finally {
			savingWorkspace = false;
		}
	}

	// GitHub Authentication
	async function handleGitHubLogin() {
		if (!githubToken.trim()) {
			error = 'Please enter a GitHub token';
			return;
		}
		githubLoginLoading = true;
		error = '';
		try {
			const result = await api.post<{success: boolean, message: string, error?: string}>('/auth/github/login', { token: githubToken });
			if (result.success) {
				githubToken = '';
				await auth.checkAuth();
				await loadAuthStatus();
			} else {
				error = result.error || result.message;
			}
		} catch (e: any) {
			error = e.detail || 'GitHub login failed';
		}
		githubLoginLoading = false;
	}

	async function handleGitHubLogout() {
		try {
			await api.post('/auth/github/logout');
			githubUser = null;
			await auth.checkAuth();
		} catch (e: any) {
			error = e.detail || 'GitHub logout failed';
		}
	}

	// Claude Code Authentication
	async function handleClaudeLogin(forceReauth: boolean = false) {
		claudeLoginLoading = true;
		claudeOAuthUrl = null;
		claudeAuthCode = '';
		error = '';
		try {
			const result = await api.post<{success: boolean, oauth_url?: string, already_authenticated?: boolean, message: string, error?: string}>('/auth/claude/login', { force_reauth: forceReauth });
			if (result.already_authenticated) {
				await auth.checkAuth();
				claudeLoginLoading = false;
				return;
			}
			if (result.oauth_url) {
				claudeOAuthUrl = result.oauth_url;
			} else {
				error = result.error || result.message || 'Failed to start OAuth login';
			}
		} catch (e: any) {
			error = e.detail || 'Claude login failed';
		}
		claudeLoginLoading = false;
	}

	async function handleClaudeReconnect() {
		// Force re-authentication by deleting credentials and starting fresh
		await handleClaudeLogin(true);
	}

	async function completeClaudeLogin() {
		if (!claudeAuthCode.trim()) {
			error = 'Please enter the authorization code';
			return;
		}
		claudeCompletingLogin = true;
		error = '';
		try {
			const result = await api.post<{success: boolean, message: string, authenticated?: boolean, error?: string}>('/auth/claude/complete', { code: claudeAuthCode.trim() });
			if (result.success && result.authenticated) {
				claudeOAuthUrl = null;
				claudeAuthCode = '';
				await auth.checkAuth();
			} else {
				error = result.error || result.message || 'Authentication failed';
			}
		} catch (e: any) {
			error = e.detail || 'Failed to complete Claude login';
		}
		claudeCompletingLogin = false;
	}

	function cancelClaudeLogin() {
		claudeOAuthUrl = null;
		claudeAuthCode = '';
	}

	async function handleClaudeLogout() {
		try {
			await api.post('/auth/claude/logout');
			await auth.checkAuth();
		} catch (e: any) {
			error = e.detail || 'Claude logout failed';
		}
	}

	function resetForm() {
		formData = {
			name: '',
			description: '',
			project_id: '',
			profile_id: ''
		};
		editingUser = null;
		showCreateForm = false;
	}

	function openCreateForm() {
		resetForm();
		showCreateForm = true;
		newlyCreatedKey = null;
	}

	function openEditForm(user: ApiUser) {
		editingUser = user;
		formData = {
			name: user.name,
			description: user.description || '',
			project_id: user.project_id || '',
			profile_id: user.profile_id || ''
		};
		showCreateForm = true;
		newlyCreatedKey = null;
	}

	async function handleSubmit() {
		if (!formData.name.trim()) {
			error = 'Name is required';
			return;
		}

		error = '';
		try {
			if (editingUser) {
				await api.put(`/api-users/${editingUser.id}`, {
					name: formData.name,
					description: formData.description || null,
					project_id: formData.project_id || null,
					profile_id: formData.profile_id || null
				});
			} else {
				const result = await api.post<ApiUserWithKey>('/api-users', {
					name: formData.name,
					description: formData.description || null,
					project_id: formData.project_id || null,
					profile_id: formData.profile_id || null
				});
				newlyCreatedKey = result.api_key;
			}
			await loadData();
			if (!newlyCreatedKey) {
				resetForm();
			}
		} catch (e: any) {
			error = e.detail || 'Failed to save API user';
		}
	}

	async function toggleActive(user: ApiUser) {
		try {
			await api.put(`/api-users/${user.id}`, {
				is_active: !user.is_active
			});
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to update user';
		}
	}

	async function regenerateKey(userId: string) {
		if (!confirm('Are you sure you want to regenerate this API key? The old key will stop working immediately.')) {
			return;
		}

		try {
			const result = await api.post<ApiUserWithKey>(`/api-users/${userId}/regenerate-key`);
			regeneratedKey = result.api_key;
			// Show in a modal or alert
			alert(`New API Key (copy now - won't be shown again):\n\n${result.api_key}`);
			regeneratedKey = null;
		} catch (e: any) {
			error = e.detail || 'Failed to regenerate key';
		}
	}

	async function deleteUser(userId: string) {
		if (!confirm('Are you sure you want to delete this API user? This cannot be undone.')) {
			return;
		}

		try {
			await api.delete(`/api-users/${userId}`);
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete user';
		}
	}

	function copyToClipboard(text: string) {
		navigator.clipboard.writeText(text);
	}

	function formatDate(dateStr: string | null): string {
		if (!dateStr) return 'Never';
		const date = new Date(dateStr);
		return date.toLocaleString();
	}

	async function handleLogout() {
		await auth.logout();
		goto('/login');
	}

	const tabs: { id: SettingsTab; label: string; icon: string }[] = [
		{ id: 'general', label: 'General', icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z' },
		{ id: 'authentication', label: 'Authentication', icon: 'M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z' },
		{ id: 'api-users', label: 'API Users', icon: 'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z' },
		{ id: 'integrations', label: 'Integrations', icon: 'M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z' }
	];
</script>

<svelte:head>
	<title>Settings - AI Hub</title>
</svelte:head>

{#if !$isAdmin}
	<!-- Show access denied for non-admin users -->
	<div class="min-h-screen bg-[var(--color-bg)] flex items-center justify-center">
		<div class="card p-8 text-center max-w-md">
			<div class="text-4xl mb-4">🔒</div>
			<h1 class="text-xl font-bold text-white mb-2">Admin Access Required</h1>
			<p class="text-gray-400 mb-4">
				This page is only available to administrators.
			</p>
			<a href="/" class="btn btn-primary">Return to Chat</a>
		</div>
	</div>
{:else}
	<div class="min-h-screen bg-background flex flex-col">
		<!-- Header -->
		<header class="bg-card border-b border-border px-4 py-3 shrink-0">
			<div class="max-w-6xl mx-auto flex items-center justify-between">
				<div class="flex items-center gap-3">
					<!-- Mobile menu toggle -->
					<button
						on:click={toggleSidebar}
						class="md:hidden p-2 -ml-2 text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg transition-colors"
						aria-label="Toggle menu"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
						</svg>
					</button>
					<a href="/" class="text-muted-foreground hover:text-foreground transition-colors">
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
						</svg>
					</a>
					<h1 class="text-lg font-bold text-foreground">Settings</h1>
				</div>
				<div class="flex items-center gap-4">
					<span class="text-sm text-muted-foreground hidden sm:inline">{$username}</span>
					<button on:click={handleLogout} class="text-sm text-muted-foreground hover:text-foreground transition-colors">
						Logout
					</button>
				</div>
			</div>
		</header>

		<div class="flex-1 flex overflow-hidden relative">
			<!-- Mobile sidebar backdrop -->
			{#if sidebarOpen}
				<button
					class="md:hidden fixed inset-0 bg-black/50 z-40 transition-opacity"
					on:click={closeSidebar}
					aria-label="Close menu"
				></button>
			{/if}

			<!-- Sidebar Navigation -->
			<nav class="
				w-64 md:w-56 bg-card border-r border-border p-4 shrink-0 overflow-y-auto
				fixed md:relative inset-y-0 left-0 z-50 md:z-auto
				transform transition-transform duration-200 ease-in-out
				{sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}
				md:transform-none
				pt-4 md:pt-4
			">
				<!-- Mobile close button -->
				<div class="flex items-center justify-between mb-4 md:hidden">
					<h2 class="text-lg font-semibold text-foreground">Menu</h2>
					<button
						on:click={closeSidebar}
						class="p-2 text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg transition-colors"
						aria-label="Close menu"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<ul class="space-y-1">
					{#each tabs as tab}
						<li>
							<button
								on:click={() => selectTab(tab.id)}
								class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all {activeTab === tab.id ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-accent'}"
							>
								<svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d={tab.icon} />
								</svg>
								<span>{tab.label}</span>
							</button>
						</li>
					{/each}
				</ul>
			</nav>

			<!-- Main Content -->
			<main class="flex-1 overflow-y-auto p-6">
				<div class="max-w-4xl mx-auto">
					{#if error}
						<div class="bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 rounded-lg mb-6">
							{error}
							<button on:click={() => error = ''} class="ml-2 hover:opacity-70">&times;</button>
						</div>
					{/if}

					<!-- General Tab -->
					{#if activeTab === 'general'}
						<div class="space-y-6">
							<div>
								<h2 class="text-xl font-bold text-foreground mb-1">General Settings</h2>
								<p class="text-sm text-muted-foreground">Configure your workspace and application preferences.</p>
							</div>

							<!-- Workspace Configuration -->
							<section class="card p-6">
								<h3 class="text-lg font-semibold text-foreground mb-4 flex items-center gap-2">
									<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
									</svg>
									Workspace Folder
								</h3>

								{#if workspaceConfig === null}
									<div class="flex items-center gap-2 text-muted-foreground">
										<span class="inline-block animate-spin">&#9696;</span>
										<span>Loading workspace configuration...</span>
									</div>
								{:else if workspaceConfig?.is_local_mode}
									<p class="text-sm text-muted-foreground mb-4">
										Configure where your projects and files are stored on your computer.
									</p>

									<div class="space-y-4">
										<div>
											<label for="workspacePath" class="block text-sm font-medium text-foreground mb-1.5">
												Workspace Path
											</label>
											<div class="flex gap-2">
												<input
													type="text"
													id="workspacePath"
													bind:value={workspacePath}
													on:input={handleWorkspaceInput}
													class="input font-mono text-sm flex-1"
													placeholder="Enter workspace path"
												/>
												<button
													on:click={saveWorkspace}
													disabled={savingWorkspace || validatingWorkspace || !workspaceValidation?.valid || workspacePath === workspaceConfig?.workspace_path}
													class="btn btn-primary whitespace-nowrap"
												>
													{#if savingWorkspace}
														<span class="inline-block animate-spin mr-2">&#9696;</span>
													{/if}
													Save
												</button>
											</div>
											<p class="text-muted-foreground text-xs mt-1.5">
												Full path to your projects folder
											</p>
										</div>

										{#if validatingWorkspace}
											<div class="text-muted-foreground text-sm flex items-center gap-2">
												<span class="inline-block animate-spin">&#9696;</span>
												Validating path...
											</div>
										{:else if workspaceValidation && workspacePath !== workspaceConfig?.workspace_path}
											{#if workspaceValidation.valid}
												<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm flex items-center gap-2">
													<span>✓</span>
													<span>
														{workspaceValidation.exists ? 'Directory exists and is writable' : 'Directory will be created'}
													</span>
												</div>
											{:else}
												<div class="bg-destructive/10 border border-destructive/30 text-destructive px-3 py-2 rounded-lg text-sm">
													{workspaceError || 'Invalid path'}
												</div>
											{/if}
										{/if}

										{#if workspaceError && !workspaceValidation}
											<div class="bg-destructive/10 border border-destructive/30 text-destructive px-3 py-2 rounded-lg text-sm">
												{workspaceError}
											</div>
										{/if}

										{#if workspaceSuccess}
											<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm flex items-center gap-2">
												<span>✓</span>
												<span>{workspaceSuccess}</span>
											</div>
										{/if}
									</div>
								{:else}
									<!-- Docker mode - show informational message -->
									<div class="flex items-start gap-3">
										<span class="text-blue-400 text-xl">🐳</span>
										<div>
											<h4 class="text-blue-300 font-medium mb-1">Docker Mode</h4>
											<p class="text-muted-foreground text-sm mb-2">
												AI Hub is running in Docker. The workspace is managed by Docker volume mounts.
											</p>
											<p class="text-muted-foreground/70 text-xs">
												Current workspace: <code class="bg-muted px-2 py-0.5 rounded">{workspaceConfig?.workspace_path || '/workspace'}</code>
											</p>
											<p class="text-muted-foreground/50 text-xs mt-2">
												To use a custom workspace folder, run AI Hub in local mode on your PC.
											</p>
										</div>
									</div>
								{/if}
							</section>
						</div>
					{/if}

					<!-- Authentication Tab -->
					{#if activeTab === 'authentication'}
						<div class="space-y-6">
							<div>
								<h2 class="text-xl font-bold text-foreground mb-1">Service Authentication</h2>
								<p class="text-sm text-muted-foreground">Connect Claude Code and GitHub CLI to enable AI features and repository management.</p>
							</div>

							<div class="grid gap-4">
								<!-- Claude Code Auth -->
								<div class="card p-6">
									<div class="flex items-center justify-between mb-4">
										<div class="flex items-center gap-3">
											<div class="w-10 h-10 bg-orange-500/15 rounded-lg flex items-center justify-center">
												<svg class="w-5 h-5 text-orange-400" viewBox="0 0 24 24" fill="currentColor">
													<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
												</svg>
											</div>
											<div>
												<span class="font-semibold text-foreground">Claude Code</span>
												<p class="text-xs text-muted-foreground">AI-powered code assistance</p>
											</div>
										</div>
										{#if $claudeAuthenticated}
											<span class="text-xs px-2.5 py-1 rounded-full bg-success/15 text-success font-medium">Connected</span>
										{:else}
											<span class="text-xs px-2.5 py-1 rounded-full bg-warning/15 text-warning font-medium">Not Connected</span>
										{/if}
									</div>

									{#if $claudeAuthenticated}
										<p class="text-sm text-muted-foreground mb-4">Claude Code is authenticated and ready to use.</p>
										<div class="flex gap-2">
											<button on:click={handleClaudeReconnect} disabled={claudeLoginLoading} class="btn btn-primary text-sm flex-1">
												{#if claudeLoginLoading}
													<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full mr-2"></span>
												{/if}
												Reconnect
											</button>
											<button on:click={handleClaudeLogout} class="btn btn-secondary text-sm flex-1">
												Disconnect
											</button>
										</div>
										<p class="text-xs text-muted-foreground mt-3">Use Reconnect if your session has expired.</p>
									{:else if claudeOAuthUrl}
										<div class="space-y-3">
											<p class="text-sm text-muted-foreground">Step 1: Open the login page in your browser:</p>
											<a href={claudeOAuthUrl} target="_blank" rel="noopener noreferrer" class="btn btn-primary text-sm w-full flex items-center justify-center gap-2">
												<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
												</svg>
												Open Login Page
											</a>
											<p class="text-sm text-muted-foreground">Step 2: After authenticating, paste the code here:</p>
											<input
												type="text"
												bind:value={claudeAuthCode}
												placeholder="Paste authorization code here"
												class="input text-sm"
											/>
											<button
												on:click={completeClaudeLogin}
												disabled={claudeCompletingLogin || !claudeAuthCode.trim()}
												class="btn btn-primary text-sm w-full"
											>
												{#if claudeCompletingLogin}
													<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full mr-2"></span>
												{/if}
												Complete Login
											</button>
											<button on:click={cancelClaudeLogin} class="text-xs text-muted-foreground hover:text-foreground w-full text-center">
												Cancel
											</button>
										</div>
									{:else}
										<p class="text-sm text-muted-foreground mb-4">Connect to enable AI-powered code assistance.</p>
										<button on:click={() => handleClaudeLogin()} disabled={claudeLoginLoading} class="btn btn-primary text-sm w-full">
											{#if claudeLoginLoading}
												<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full mr-2"></span>
											{/if}
											Connect Claude Code
										</button>
									{/if}
								</div>

								<!-- GitHub CLI Auth -->
								<div class="card p-6">
									<div class="flex items-center justify-between mb-4">
										<div class="flex items-center gap-3">
											<div class="w-10 h-10 bg-white/10 rounded-lg flex items-center justify-center">
												<svg class="w-5 h-5 text-white" viewBox="0 0 24 24" fill="currentColor">
													<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
												</svg>
											</div>
											<div>
												<span class="font-semibold text-foreground">GitHub CLI</span>
												<p class="text-xs text-muted-foreground">Repository management</p>
											</div>
										</div>
										{#if $githubAuthenticated}
											<span class="text-xs px-2.5 py-1 rounded-full bg-success/15 text-success font-medium">Connected</span>
										{:else}
											<span class="text-xs px-2.5 py-1 rounded-full bg-warning/15 text-warning font-medium">Not Connected</span>
										{/if}
									</div>

									{#if $githubAuthenticated}
										<p class="text-sm text-muted-foreground mb-4">
											Connected as <span class="text-foreground font-medium">{githubUser || 'GitHub User'}</span>
										</p>
										<button on:click={handleGitHubLogout} class="btn btn-secondary text-sm w-full">
											Disconnect
										</button>
									{:else}
										<p class="text-sm text-muted-foreground mb-4">Connect to enable repository management.</p>
										<div class="space-y-3">
											<input
												type="password"
												bind:value={githubToken}
												placeholder="GitHub Personal Access Token"
												class="input text-sm"
											/>
											<button on:click={handleGitHubLogin} disabled={githubLoginLoading} class="btn btn-primary text-sm w-full">
												{#if githubLoginLoading}
													<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full mr-2"></span>
												{/if}
												Connect GitHub
											</button>
											<p class="text-xs text-muted-foreground">
												<a href="https://github.com/settings/tokens/new?scopes=repo,read:org,gist,workflow" target="_blank" rel="noopener noreferrer" class="text-primary hover:underline">
													Create a token
												</a>
												with repo, read:org, gist, workflow scopes
											</p>
										</div>
									{/if}
								</div>
							</div>
						</div>
					{/if}

					<!-- API Users Tab -->
					{#if activeTab === 'api-users'}
						<div class="space-y-6">
							<div class="flex items-center justify-between">
								<div>
									<h2 class="text-xl font-bold text-foreground mb-1">API Users</h2>
									<p class="text-sm text-muted-foreground">Create API users for programmatic access. Each user gets an API key.</p>
								</div>
								<button on:click={openCreateForm} class="btn btn-primary flex items-center gap-2 p-2 sm:px-4 sm:py-2">
									<svg class="w-5 h-5 sm:w-4 sm:h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
									</svg>
									<span class="hidden sm:inline">Create API User</span>
								</button>
							</div>

							{#if loading}
								<div class="text-center py-12">
									<div class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto"></div>
								</div>
							{:else if apiUsers.length === 0}
								<div class="card p-12 text-center">
									<div class="w-12 h-12 bg-muted rounded-full flex items-center justify-center mx-auto mb-4">
										<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
										</svg>
									</div>
									<p class="text-foreground font-medium mb-1">No API users yet</p>
									<p class="text-sm text-muted-foreground">Create an API user to allow external applications to access AI Hub.</p>
								</div>
							{:else}
								<div class="space-y-3">
									{#each apiUsers as user}
										<div class="card p-4">
											<div class="flex items-start justify-between">
												<div class="flex-1 min-w-0">
													<div class="flex items-center gap-2 mb-1">
														<span class="font-medium text-foreground">{user.name}</span>
														<span class="text-xs px-2 py-0.5 rounded-full {user.is_active ? 'bg-success/15 text-success' : 'bg-destructive/15 text-destructive'}">
															{user.is_active ? 'Active' : 'Inactive'}
														</span>
													</div>
													{#if user.description}
														<p class="text-sm text-muted-foreground mb-2">{user.description}</p>
													{/if}
													<div class="flex flex-wrap gap-4 text-xs text-muted-foreground">
														<span>
															Profile: <span class="text-foreground/80">
																{profiles.find(p => p.id === user.profile_id)?.name || 'Any'}
															</span>
														</span>
														<span>
															Project: <span class="text-foreground/80">
																{projects.find(p => p.id === user.project_id)?.name || 'None'}
															</span>
														</span>
														<span>Created: {formatDate(user.created_at)}</span>
														<span>Last used: {formatDate(user.last_used_at)}</span>
													</div>
												</div>
												<div class="flex items-center gap-1 ml-4">
													<button
														on:click={() => openEditForm(user)}
														class="p-2 text-muted-foreground hover:text-foreground rounded-lg hover:bg-accent transition-colors"
														title="Edit"
													>
														<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
														</svg>
													</button>
													<button
														on:click={() => regenerateKey(user.id)}
														class="p-2 text-muted-foreground hover:text-warning rounded-lg hover:bg-accent transition-colors"
														title="Regenerate Key"
													>
														<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
														</svg>
													</button>
													<button
														on:click={() => toggleActive(user)}
														class="p-2 text-muted-foreground hover:text-primary rounded-lg hover:bg-accent transition-colors"
														title={user.is_active ? 'Deactivate' : 'Activate'}
													>
														<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															{#if user.is_active}
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
															{:else}
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
															{/if}
														</svg>
													</button>
													<button
														on:click={() => deleteUser(user.id)}
														class="p-2 text-muted-foreground hover:text-destructive rounded-lg hover:bg-accent transition-colors"
														title="Delete"
													>
														<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
														</svg>
													</button>
												</div>
											</div>
										</div>
									{/each}
								</div>
							{/if}

							<!-- API Usage Instructions -->
							<section class="card p-6 mt-6">
								<h3 class="text-lg font-semibold text-foreground mb-3">API Usage</h3>
								<p class="text-sm text-muted-foreground mb-4">
									Use the API key in the Authorization header to make requests:
								</p>
								<pre class="bg-muted p-4 rounded-lg text-sm overflow-x-auto mb-4"><code class="text-foreground/90">curl -X POST http://localhost:8080/api/v1/conversation/stream \
  -H "Authorization: Bearer aih_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{{"prompt": "Hello, Claude!"}}'</code></pre>
								<p class="text-xs text-muted-foreground">
									When using an API key, the configured project and profile for that user will be used automatically.
								</p>
							</section>
						</div>
					{/if}

					<!-- Integrations Tab -->
					{#if activeTab === 'integrations'}
						<div class="space-y-6">
							<div>
								<h2 class="text-xl font-bold text-foreground mb-1">Integrations</h2>
								<p class="text-sm text-muted-foreground">Connect external services to extend AI Hub functionality.</p>
							</div>

							<!-- OpenAI Integration -->
							<div class="card p-6">
								<div class="flex items-center justify-between mb-4">
									<div class="flex items-center gap-3">
										<div class="w-10 h-10 bg-emerald-500/15 rounded-lg flex items-center justify-center">
											<svg class="w-6 h-6 text-emerald-400" viewBox="0 0 24 24" fill="currentColor">
												<path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.8956zm16.0993 3.8558L12.6 8.3829l2.02-1.1638a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.4066-.6567zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/>
											</svg>
										</div>
										<div>
											<span class="font-semibold text-foreground">OpenAI</span>
											<p class="text-xs text-muted-foreground">Voice-to-text transcription (Whisper)</p>
										</div>
									</div>
									{#if openaiApiKeyMasked}
										<span class="text-xs px-2.5 py-1 rounded-full bg-success/15 text-success font-medium">Configured</span>
									{:else}
										<span class="text-xs px-2.5 py-1 rounded-full bg-muted text-muted-foreground font-medium">Not Configured</span>
									{/if}
								</div>

								<p class="text-sm text-muted-foreground mb-4">
									Add your OpenAI API key to enable voice-to-text transcription using the Whisper model.
									This allows you to dictate messages instead of typing.
								</p>

								{#if openaiKeySuccess}
									<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm flex items-center gap-2 mb-4">
										<span>✓</span>
										<span>{openaiKeySuccess}</span>
									</div>
								{/if}

								{#if openaiKeyError}
									<div class="bg-destructive/10 border border-destructive/30 text-destructive px-3 py-2 rounded-lg text-sm mb-4">
										{openaiKeyError}
									</div>
								{/if}

								{#if openaiApiKeyMasked}
									<div class="flex items-center gap-3 mb-4 p-3 bg-muted rounded-lg">
										<span class="text-sm text-muted-foreground">Current key:</span>
										<code class="text-sm text-foreground font-mono">{openaiApiKeyMasked}</code>
									</div>
								{/if}

								<div class="space-y-3">
									<div>
										<label for="openaiKey" class="block text-sm font-medium text-foreground mb-1.5">
											{openaiApiKeyMasked ? 'Replace API Key' : 'API Key'}
										</label>
										<input
											type="password"
											id="openaiKey"
											bind:value={openaiApiKey}
											placeholder="sk-..."
											class="input text-sm font-mono"
										/>
									</div>
									<div class="flex gap-2">
										<button
											on:click={saveOpenaiApiKey}
											disabled={savingOpenaiKey || !openaiApiKey.trim()}
											class="btn btn-primary text-sm flex-1"
										>
											{#if savingOpenaiKey}
												<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full mr-2"></span>
											{/if}
											{openaiApiKeyMasked ? 'Update Key' : 'Save Key'}
										</button>
										{#if openaiApiKeyMasked}
											<button
												on:click={removeOpenaiApiKey}
												disabled={savingOpenaiKey}
												class="btn btn-secondary text-sm"
											>
												Remove
											</button>
										{/if}
									</div>
									<p class="text-xs text-muted-foreground">
										<a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener noreferrer" class="text-primary hover:underline">
											Get an API key
										</a>
										from OpenAI. Whisper transcription costs ~$0.006/minute.
									</p>
								</div>
							</div>

							<!-- Image Generation (Nano Banana) Integration -->
							<div class="card p-6">
								<div class="flex items-center justify-between mb-4">
									<div class="flex items-center gap-3">
										<div class="w-10 h-10 bg-yellow-500/15 rounded-lg flex items-center justify-center text-2xl">
											🍌
										</div>
										<div>
											<span class="font-semibold text-foreground">Nano Banana</span>
											<p class="text-xs text-muted-foreground">AI Image Generation (Google Gemini)</p>
										</div>
									</div>
									{#if imageApiKeyMasked}
										<span class="text-xs px-2.5 py-1 rounded-full bg-success/15 text-success font-medium">Configured</span>
									{:else}
										<span class="text-xs px-2.5 py-1 rounded-full bg-muted text-muted-foreground font-medium">Not Configured</span>
									{/if}
								</div>

								<p class="text-sm text-muted-foreground mb-4">
									Generate images using Google's Gemini image models (codename: Nano Banana).
									Create stunning visuals with AI-powered image generation.
								</p>

								{#if imageConfigSuccess}
									<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm flex items-center gap-2 mb-4">
										<span>✓</span>
										<span>{imageConfigSuccess}</span>
									</div>
								{/if}

								{#if imageConfigError}
									<div class="bg-destructive/10 border border-destructive/30 text-destructive px-3 py-2 rounded-lg text-sm mb-4">
										{imageConfigError}
									</div>
								{/if}

								{#if imageApiKeyMasked}
									<div class="flex items-center gap-3 mb-4 p-3 bg-muted rounded-lg">
										<span class="text-sm text-muted-foreground">Current key:</span>
										<code class="text-sm text-foreground font-mono">{imageApiKeyMasked}</code>
										{#if imageModel}
											<span class="text-xs px-2 py-0.5 rounded bg-primary/15 text-primary ml-auto">
												{imageModels.find(m => m.id === imageModel)?.name || imageModel}
											</span>
										{/if}
									</div>
								{/if}

								<div class="space-y-4">
									<!-- Model Selection -->
									<div>
										<label for="imageModel" class="block text-sm font-medium text-foreground mb-1.5">
											Model
										</label>
										<select
											id="imageModel"
											bind:value={selectedImageModel}
											class="input text-sm"
										>
											{#each imageModels as model}
												<option value={model.id}>
													{model.name} (${model.price_per_image}/image)
												</option>
											{/each}
										</select>
										{#if selectedImageModel}
											<p class="text-xs text-muted-foreground mt-1">
												{imageModels.find(m => m.id === selectedImageModel)?.description || ''}
											</p>
										{/if}
									</div>

									<!-- API Key Input -->
									<div>
										<label for="imageApiKey" class="block text-sm font-medium text-foreground mb-1.5">
											{imageApiKeyMasked ? 'Replace API Key' : 'Google AI API Key'}
										</label>
										<input
											type="password"
											id="imageApiKey"
											bind:value={imageApiKey}
											placeholder="AIza..."
											class="input text-sm font-mono"
										/>
									</div>

									<!-- Action Buttons -->
									<div class="flex gap-2">
										<button
											on:click={saveImageConfig}
											disabled={savingImageConfig || (!imageApiKey.trim() && !imageApiKeyMasked)}
											class="btn btn-primary text-sm flex-1"
										>
											{#if savingImageConfig}
												<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full mr-2"></span>
											{/if}
											{imageApiKeyMasked ? 'Update Configuration' : 'Save Configuration'}
										</button>
										{#if imageApiKeyMasked}
											<button
												on:click={removeImageConfig}
												disabled={savingImageConfig}
												class="btn btn-secondary text-sm"
											>
												Remove
											</button>
										{/if}
									</div>

									<!-- Test Generation -->
									{#if imageApiKeyMasked}
										<div class="pt-2 border-t border-border">
											<button
												on:click={testImageGeneration}
												disabled={testingImage}
												class="btn btn-secondary text-sm w-full"
											>
												{#if testingImage}
													<span class="animate-spin inline-block w-4 h-4 border-2 border-current border-t-transparent rounded-full mr-2"></span>
													Generating test image...
												{:else}
													🎨 Test Image Generation
												{/if}
											</button>

											{#if testImageResult}
												{#if testImageResult.success && testImageResult.image_base64}
													<div class="mt-3 p-3 bg-muted rounded-lg">
														<p class="text-xs text-muted-foreground mb-2">Test image generated successfully:</p>
														<img
															src="data:{testImageResult.mime_type || 'image/png'};base64,{testImageResult.image_base64}"
															alt="Generated test image"
															class="rounded-lg max-w-full h-auto max-h-64 mx-auto"
														/>
													</div>
												{:else if testImageResult.error}
													<div class="mt-3 p-3 bg-destructive/10 rounded-lg">
														<p class="text-xs text-destructive">{testImageResult.error}</p>
													</div>
												{/if}
											{/if}
										</div>
									{/if}

									<p class="text-xs text-muted-foreground">
										<a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer" class="text-primary hover:underline">
											Get an API key
										</a>
										from Google AI Studio. Image generation costs ~$0.039/image.
									</p>
								</div>
							</div>

							<!-- Future integrations placeholder -->
							<div class="card p-6 border-dashed opacity-60">
								<div class="flex items-center gap-3 mb-2">
									<div class="w-10 h-10 bg-muted rounded-lg flex items-center justify-center">
										<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
										</svg>
									</div>
									<div>
										<span class="font-semibold text-muted-foreground">More Integrations Coming</span>
										<p class="text-xs text-muted-foreground">Video, voice, and more AI tools coming soon</p>
									</div>
								</div>
							</div>
						</div>
					{/if}
				</div>
			</main>
		</div>
	</div>

	<!-- Create/Edit Modal -->
	{#if showCreateForm}
		<div class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
			<div class="card w-full max-w-lg shadow-2xl">
				<div class="p-4 border-b border-border flex items-center justify-between">
					<h2 class="text-lg font-bold text-foreground">
						{editingUser ? 'Edit API User' : 'Create API User'}
					</h2>
					<button
						class="text-muted-foreground hover:text-foreground transition-colors"
						on:click={resetForm}
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<div class="p-4 space-y-4">
					{#if newlyCreatedKey}
						<!-- Show newly created key -->
						<div class="bg-success/10 border border-success/30 p-4 rounded-lg">
							<p class="text-success font-medium mb-2">API User created successfully!</p>
							<p class="text-sm text-muted-foreground mb-3">Copy this API key now - it won't be shown again:</p>
							<div class="flex items-center gap-2">
								<code class="flex-1 bg-muted px-3 py-2 rounded-lg text-sm text-foreground break-all font-mono">
									{newlyCreatedKey}
								</code>
								<button
									on:click={() => copyToClipboard(newlyCreatedKey || '')}
									class="btn btn-secondary shrink-0"
								>
									Copy
								</button>
							</div>
						</div>
						<button on:click={resetForm} class="btn btn-primary w-full">Done</button>
					{:else}
						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Name *</label>
							<input
								bind:value={formData.name}
								class="input"
								placeholder="My Application"
							/>
						</div>

						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Description</label>
							<textarea
								bind:value={formData.description}
								class="input"
								rows="2"
								placeholder="Optional description"
							></textarea>
						</div>

						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Project</label>
							<select bind:value={formData.project_id} class="input">
								<option value="">Default Workspace</option>
								{#each projects as project}
									<option value={project.id}>{project.name}</option>
								{/each}
							</select>
							<p class="text-xs text-muted-foreground mt-1">Restrict this user to a specific project workspace</p>
						</div>

						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Profile</label>
							<select bind:value={formData.profile_id} class="input">
								<option value="">Any Profile</option>
								{#each profiles as profile}
									<option value={profile.id}>{profile.name}</option>
								{/each}
							</select>
							<p class="text-xs text-muted-foreground mt-1">Restrict this user to use a specific agent profile</p>
						</div>

						<div class="flex gap-2 pt-2">
							<button on:click={handleSubmit} class="btn btn-primary flex-1">
								{editingUser ? 'Save Changes' : 'Create User'}
							</button>
							<button on:click={resetForm} class="btn btn-secondary flex-1">Cancel</button>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}
{/if}
