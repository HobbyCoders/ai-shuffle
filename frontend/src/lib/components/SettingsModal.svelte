<script lang="ts">
	/**
	 * SettingsModal - Settings modal using UniversalModal
	 *
	 * Provides a tabbed settings interface similar to the reference design:
	 * - General: Workspace configuration
	 * - Appearance: Theme and display settings
	 * - Profiles: Profile management (future)
	 * - Advanced: Security, authentication, API users
	 */
	import { onMount } from 'svelte';
	import UniversalModal from './UniversalModal.svelte';
	import { auth, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import { api } from '$lib/api/client';
	import type { WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig, changePassword } from '$lib/api/auth';

	interface Props {
		open: boolean;
		onClose: () => void;
	}

	let { open, onClose }: Props = $props();

	// Tab management
	type SettingsTab = 'general' | 'appearance' | 'profiles' | 'advanced';
	let activeTab = $state<SettingsTab>('general');

	const tabs = [
		{ id: 'general', label: 'General' },
		{ id: 'appearance', label: 'Appearance' },
		{ id: 'profiles', label: 'Profiles' },
		{ id: 'advanced', label: 'Advanced' }
	];

	// Workspace state
	let workspaceConfig = $state<WorkspaceConfig | null>(null);
	let workspacePath = $state('');
	let workspaceValidation = $state<WorkspaceValidation | null>(null);
	let validatingWorkspace = $state(false);
	let savingWorkspace = $state(false);
	let workspaceError = $state('');
	let workspaceSuccess = $state('');

	// Appearance state (placeholder for future)
	let theme = $state<'dark' | 'light' | 'system'>('dark');
	let fontSize = $state<'small' | 'medium' | 'large'>('medium');

	// Password change state
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let changingPassword = $state(false);
	let passwordChangeError = $state('');
	let passwordChangeSuccess = $state('');

	// Auth state
	let githubToken = $state('');
	let githubLoginLoading = $state(false);
	let claudeLoginLoading = $state(false);
	let claudeOAuthUrl = $state<string | null>(null);
	let claudeAuthCode = $state('');
	let claudeCompletingLogin = $state(false);
	let githubUser = $state<string | null>(null);
	let error = $state('');

	// Integration settings
	let openaiApiKey = $state('');
	let openaiApiKeyMasked = $state('');
	let savingOpenaiKey = $state(false);
	let imageApiKey = $state('');
	let imageApiKeyMasked = $state('');
	let savingImageConfig = $state(false);

	// Model selections
	interface ImageModel {
		id: string;
		name: string;
		description: string;
		price_per_image: number;
		provider: string;
		provider_name: string;
		available: boolean;
		is_current: boolean;
	}
	interface VideoModel {
		id: string;
		name: string;
		description: string;
		price_per_second: number;
		provider: string;
		provider_name: string;
		max_duration: number;
		capabilities: string[];
		available: boolean;
		is_current: boolean;
	}
	interface AudioModel {
		id: string;
		name: string;
		description: string;
		price_display: string;
		available: boolean;
		is_current: boolean;
	}

	let imageModels = $state<ImageModel[]>([]);
	let videoModels = $state<VideoModel[]>([]);
	let ttsModels = $state<AudioModel[]>([]);
	let sttModels = $state<AudioModel[]>([]);
	let imageModel = $state('');
	let videoModel = $state('');
	let selectedImageModel = $state('');
	let selectedVideoModel = $state('');
	let currentTtsModel = $state('');
	let currentSttModel = $state('');
	let selectedTtsModel = $state('');
	let selectedSttModel = $state('');
	let updatingImageModel = $state(false);
	let savingVideoModel = $state(false);
	let savingTtsModel = $state(false);
	let savingSttModel = $state(false);
	let configSuccess = $state('');
	let configError = $state('');

	// Load data when modal opens
	$effect(() => {
		if (open) {
			loadWorkspaceConfig();
			loadAuthStatus();
			loadIntegrationSettings();
		}
	});

	async function loadWorkspaceConfig() {
		try {
			workspaceConfig = await getWorkspaceConfig();
			workspacePath = workspaceConfig.workspace_path;
		} catch (e) {
			console.error('Failed to load workspace config:', e);
		}
	}

	async function loadAuthStatus() {
		try {
			const ghStatus = await api.get<{authenticated: boolean, user: string | null}>('/auth/github/status');
			githubUser = ghStatus.user;
		} catch (e) {
			console.error('Failed to load GitHub status:', e);
		}
	}

	async function loadIntegrationSettings() {
		try {
			const settings = await api.get<{
				openai_api_key_set: boolean;
				openai_api_key_masked: string;
				image_provider: string | null;
				image_model: string | null;
				image_api_key_set: boolean;
				image_api_key_masked: string;
				video_provider: string | null;
				video_model: string | null;
				tts_model: string | null;
				stt_model: string | null;
			}>('/settings/integrations');
			openaiApiKeyMasked = settings.openai_api_key_masked;
			imageApiKeyMasked = settings.image_api_key_masked || '';
			imageModel = settings.image_model || '';
			selectedImageModel = settings.image_model || '';
			videoModel = settings.video_model || '';
			selectedVideoModel = settings.video_model || '';
			currentTtsModel = settings.tts_model || 'gpt-4o-mini-tts';
			currentSttModel = settings.stt_model || 'whisper-1';
			selectedTtsModel = currentTtsModel;
			selectedSttModel = currentSttModel;
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}

		// Load models
		try {
			const imageResponse = await api.get<{
				models: ImageModel[];
				current_model: string | null;
			}>('/settings/integrations/image/models');
			imageModels = imageResponse.models;
			if (!selectedImageModel && imageResponse.current_model) {
				selectedImageModel = imageResponse.current_model;
			}
		} catch (e) {
			console.error('Failed to load image models:', e);
		}

		try {
			const videoResponse = await api.get<{
				models: VideoModel[];
				current_model: string | null;
			}>('/settings/integrations/video/models');
			videoModels = videoResponse.models;
			if (!selectedVideoModel && videoResponse.current_model) {
				selectedVideoModel = videoResponse.current_model;
			}
		} catch (e) {
			console.error('Failed to load video models:', e);
		}

		try {
			const audioResponse = await api.get<{
				tts_models: AudioModel[];
				stt_models: AudioModel[];
				current_tts: string;
				current_stt: string;
			}>('/settings/integrations/audio/models');
			ttsModels = audioResponse.tts_models;
			sttModels = audioResponse.stt_models;
			currentTtsModel = audioResponse.current_tts;
			currentSttModel = audioResponse.current_stt;
			selectedTtsModel = currentTtsModel;
			selectedSttModel = currentSttModel;
		} catch (e) {
			console.error('Failed to load audio models:', e);
		}
	}

	// Workspace functions
	let workspaceValidateTimeout: ReturnType<typeof setTimeout>;
	function handleWorkspaceInput() {
		clearTimeout(workspaceValidateTimeout);
		workspaceSuccess = '';
		workspaceValidateTimeout = setTimeout(validateWorkspace, 500);
	}

	async function validateWorkspace() {
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

	// Password change
	async function handlePasswordChange() {
		passwordChangeError = '';
		passwordChangeSuccess = '';

		if (!currentPassword || !newPassword || !confirmPassword) {
			passwordChangeError = 'All fields are required';
			return;
		}
		if (newPassword.length < 8) {
			passwordChangeError = 'New password must be at least 8 characters';
			return;
		}
		if (newPassword !== confirmPassword) {
			passwordChangeError = 'New passwords do not match';
			return;
		}

		changingPassword = true;
		try {
			await changePassword(currentPassword, newPassword);
			passwordChangeSuccess = 'Password changed successfully';
			currentPassword = '';
			newPassword = '';
			confirmPassword = '';
		} catch (e: any) {
			passwordChangeError = e.detail || 'Failed to change password';
		} finally {
			changingPassword = false;
		}
	}

	// Auth functions
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

	async function handleClaudeLogout() {
		try {
			await api.post('/auth/claude/logout');
			await auth.checkAuth();
		} catch (e: any) {
			error = e.detail || 'Claude logout failed';
		}
	}

	// Integration functions
	async function saveOpenaiApiKey() {
		if (!openaiApiKey.trim()) return;
		savingOpenaiKey = true;
		configError = '';
		try {
			const result = await api.post<{success: boolean, masked_key: string}>('/settings/integrations/openai', {
				api_key: openaiApiKey
			});
			openaiApiKeyMasked = result.masked_key;
			openaiApiKey = '';
			configSuccess = 'OpenAI API key saved';
			setTimeout(() => configSuccess = '', 3000);
		} catch (e: any) {
			configError = e.detail || 'Failed to save API key';
		} finally {
			savingOpenaiKey = false;
		}
	}

	async function saveGeminiApiKey() {
		if (!imageApiKey.trim() && !imageApiKeyMasked) return;
		savingImageConfig = true;
		configError = '';
		try {
			const result = await api.post<{success: boolean, masked_key: string}>('/settings/integrations/image', {
				provider: 'google',
				model: selectedImageModel || 'gemini-2.5-flash-image',
				api_key: imageApiKey || imageApiKeyMasked
			});
			imageApiKeyMasked = result.masked_key;
			imageApiKey = '';
			configSuccess = 'Google API key saved';
			setTimeout(() => configSuccess = '', 3000);
		} catch (e: any) {
			configError = e.detail || 'Failed to save API key';
		} finally {
			savingImageConfig = false;
		}
	}

	async function updateImageModel(modelId: string) {
		if (!modelId || modelId === imageModel) return;
		selectedImageModel = modelId;
		updatingImageModel = true;
		try {
			const result = await api.patch<{success: boolean, model: string}>('/settings/integrations/image/model', {
				model: modelId
			});
			imageModel = result.model;
			configSuccess = 'Image model updated';
			setTimeout(() => configSuccess = '', 3000);
		} catch (e: any) {
			configError = e.detail || 'Failed to update model';
		} finally {
			updatingImageModel = false;
		}
	}

	async function updateVideoModel(modelId: string) {
		if (!modelId || modelId === videoModel) return;
		selectedVideoModel = modelId;
		savingVideoModel = true;
		try {
			const result = await api.patch<{success: boolean, model: string}>('/settings/integrations/video/model', {
				model: modelId
			});
			videoModel = result.model;
			configSuccess = 'Video model updated';
			setTimeout(() => configSuccess = '', 3000);
		} catch (e: any) {
			configError = e.detail || 'Failed to update model';
		} finally {
			savingVideoModel = false;
		}
	}

	async function updateTtsModel(modelId: string) {
		if (!modelId || modelId === currentTtsModel) return;
		selectedTtsModel = modelId;
		savingTtsModel = true;
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/tts', { model: modelId });
			currentTtsModel = modelId;
			configSuccess = 'TTS model updated';
			setTimeout(() => configSuccess = '', 3000);
		} catch (e: any) {
			configError = e.detail || 'Failed to update model';
		} finally {
			savingTtsModel = false;
		}
	}

	async function updateSttModel(modelId: string) {
		if (!modelId || modelId === currentSttModel) return;
		selectedSttModel = modelId;
		savingSttModel = true;
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/stt', { model: modelId });
			currentSttModel = modelId;
			configSuccess = 'STT model updated';
			setTimeout(() => configSuccess = '', 3000);
		} catch (e: any) {
			configError = e.detail || 'Failed to update model';
		} finally {
			savingSttModel = false;
		}
	}

	function handleTabChange(tabId: string) {
		activeTab = tabId as SettingsTab;
	}

	// Settings icon SVG path
	const settingsIcon = 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z';
</script>

<UniversalModal
	{open}
	title="Settings"
	icon={settingsIcon}
	{tabs}
	{activeTab}
	onTabChange={handleTabChange}
	{onClose}
	showFooter={false}
	size="xl"
>
	{#if activeTab === 'general'}
		<!-- General Tab -->
		<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
			<!-- Left column: Application settings -->
			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-4">Application</h3>

				<!-- Workspace -->
				<div class="py-3">
					<label class="block text-sm text-foreground mb-2">Workspace Path</label>
					{#if workspaceConfig?.is_local_mode}
						<div class="flex gap-2">
							<input
								type="text"
								bind:value={workspacePath}
								oninput={handleWorkspaceInput}
								class="flex-1 px-3 py-2.5 bg-input border border-border rounded-lg text-foreground font-mono text-sm"
								placeholder="Enter workspace path"
							/>
							<button
								onclick={saveWorkspace}
								disabled={savingWorkspace || validatingWorkspace || !workspaceValidation?.valid || workspacePath === workspaceConfig?.workspace_path}
								class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
							>
								{savingWorkspace ? '...' : 'Save'}
							</button>
						</div>
						{#if workspaceSuccess}
							<p class="text-xs text-success mt-2">{workspaceSuccess}</p>
						{/if}
						{#if workspaceError}
							<p class="text-xs text-destructive mt-2">{workspaceError}</p>
						{/if}
					{:else}
						<div class="p-3 bg-muted/30 rounded-lg text-sm text-muted-foreground">
							Docker Mode - Workspace: <code class="font-mono">{workspaceConfig?.workspace_path || '/workspace'}</code>
						</div>
					{/if}
				</div>
			</div>

			<!-- Right column -->
			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-4">Preferences</h3>

				<!-- Language selector -->
				<div class="py-3">
					<label class="block text-sm text-foreground mb-2">Language</label>
					<select class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-foreground">
						<option>English (US)</option>
						<option>Spanish</option>
						<option>French</option>
						<option>German</option>
					</select>
				</div>
			</div>
		</div>

	{:else if activeTab === 'appearance'}
		<!-- Appearance Tab -->
		<div class="grid grid-cols-1 md:grid-cols-2 gap-6">
			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-4">Theme</h3>

				<div class="py-3">
					<label class="block text-sm text-foreground mb-2">Theme</label>
					<select bind:value={theme} class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-foreground">
						<option value="dark">Dark Mode</option>
						<option value="light">Light Mode</option>
						<option value="system">System</option>
					</select>
				</div>
			</div>

			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-4">Display</h3>

				<div class="py-3">
					<label class="block text-sm text-foreground mb-2">Font Size</label>
					<div class="flex items-center gap-4">
						<span class="text-sm text-muted-foreground">{fontSize.charAt(0).toUpperCase() + fontSize.slice(1)}</span>
						<input type="range" min="0" max="2" value={fontSize === 'small' ? 0 : fontSize === 'medium' ? 1 : 2} class="flex-1 h-2 bg-primary/30 rounded-lg appearance-none cursor-pointer accent-primary" />
					</div>
				</div>
			</div>
		</div>

	{:else if activeTab === 'profiles'}
		<!-- Profiles tab - full width -->
		<div class="space-y-4">
			<p class="text-sm text-muted-foreground">
				Manage your agent profiles and their configurations.
			</p>
			<div class="text-center py-12 text-muted-foreground">
				<svg class="w-12 h-12 mx-auto mb-4 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
				</svg>
				<p>Profile management coming soon.</p>
				<p class="text-xs mt-1">Use the sidebar Profiles panel for now.</p>
			</div>
		</div>

	{:else if activeTab === 'advanced'}
		<!-- Advanced tab - Security & Authentication -->
		<div class="space-y-6">
			<!-- Success/Error messages -->
			{#if configSuccess}
				<div class="p-3 bg-success/10 border border-success/30 text-success rounded-lg text-sm flex items-center gap-2">
					<span>✓</span> {configSuccess}
				</div>
			{/if}
			{#if configError || error}
				<div class="p-3 bg-destructive/10 border border-destructive/30 text-destructive rounded-lg text-sm">
					{configError || error}
				</div>
			{/if}

			<!-- Authentication Section -->
			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-3">Authentication</h3>
				<div class="grid gap-3 sm:grid-cols-2">
					<!-- Claude Code -->
					<div class="p-4 bg-muted/30 rounded-xl border border-border">
						<div class="flex items-center justify-between mb-3">
							<div class="flex items-center gap-2">
								<div class="w-8 h-8 bg-orange-500/15 rounded-lg flex items-center justify-center">
									<svg class="w-4 h-4 text-orange-400" viewBox="0 0 24 24" fill="currentColor">
										<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93z"/>
									</svg>
								</div>
								<span class="font-medium text-foreground text-sm">Claude Code</span>
							</div>
							{#if $claudeAuthenticated}
								<span class="text-xs px-2 py-0.5 rounded-full bg-success/15 text-success">Connected</span>
							{:else}
								<span class="text-xs px-2 py-0.5 rounded-full bg-warning/15 text-warning">Not Connected</span>
							{/if}
						</div>
						{#if $claudeAuthenticated}
							<div class="flex gap-2">
								<button onclick={() => handleClaudeLogin(true)} disabled={claudeLoginLoading} class="flex-1 px-3 py-2 bg-primary text-primary-foreground rounded-lg text-xs font-medium hover:opacity-90 disabled:opacity-50">
									{claudeLoginLoading ? 'Loading...' : 'Reconnect'}
								</button>
								<button onclick={handleClaudeLogout} class="flex-1 px-3 py-2 bg-muted text-foreground rounded-lg text-xs font-medium hover:bg-accent">
									Disconnect
								</button>
							</div>
						{:else if claudeOAuthUrl}
							<div class="space-y-2">
								<a href={claudeOAuthUrl} target="_blank" class="block w-full px-3 py-2 bg-primary text-primary-foreground rounded-lg text-xs font-medium text-center hover:opacity-90">
									1. Open Login
								</a>
								<div class="flex gap-2">
									<input type="text" bind:value={claudeAuthCode} placeholder="2. Paste code" class="flex-1 px-2 py-1.5 bg-input border border-border rounded-lg text-xs" />
									<button onclick={completeClaudeLogin} disabled={claudeCompletingLogin || !claudeAuthCode.trim()} class="px-3 py-1.5 bg-primary text-primary-foreground rounded-lg text-xs font-medium disabled:opacity-50">
										OK
									</button>
								</div>
							</div>
						{:else}
							<button onclick={() => handleClaudeLogin()} disabled={claudeLoginLoading} class="w-full px-3 py-2 bg-primary text-primary-foreground rounded-lg text-xs font-medium hover:opacity-90 disabled:opacity-50">
								{claudeLoginLoading ? 'Loading...' : 'Connect'}
							</button>
						{/if}
					</div>

					<!-- GitHub -->
					<div class="p-4 bg-muted/30 rounded-xl border border-border">
						<div class="flex items-center justify-between mb-3">
							<div class="flex items-center gap-2">
								<div class="w-8 h-8 bg-white/10 rounded-lg flex items-center justify-center">
									<svg class="w-4 h-4 text-white" viewBox="0 0 24 24" fill="currentColor">
										<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
									</svg>
								</div>
								<span class="font-medium text-foreground text-sm">GitHub</span>
							</div>
							{#if $githubAuthenticated}
								<span class="text-xs px-2 py-0.5 rounded-full bg-success/15 text-success">Connected</span>
							{:else}
								<span class="text-xs px-2 py-0.5 rounded-full bg-warning/15 text-warning">Not Connected</span>
							{/if}
						</div>
						{#if $githubAuthenticated}
							<div class="text-xs text-muted-foreground mb-2">{githubUser}</div>
							<button onclick={handleGitHubLogout} class="w-full px-3 py-2 bg-muted text-foreground rounded-lg text-xs font-medium hover:bg-accent">
								Disconnect
							</button>
						{:else}
							<div class="space-y-2">
								<input type="password" bind:value={githubToken} placeholder="Personal Access Token" class="w-full px-2 py-1.5 bg-input border border-border rounded-lg text-xs" />
								<button onclick={handleGitHubLogin} disabled={githubLoginLoading} class="w-full px-3 py-2 bg-primary text-primary-foreground rounded-lg text-xs font-medium hover:opacity-90 disabled:opacity-50">
									{githubLoginLoading ? 'Loading...' : 'Connect'}
								</button>
							</div>
						{/if}
					</div>
				</div>
			</div>

			<!-- API Keys Section -->
			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-3">API Keys</h3>
				<div class="grid gap-3 sm:grid-cols-2">
					<!-- OpenAI -->
					<div class="p-4 bg-muted/30 rounded-xl border border-border">
						<div class="flex items-center justify-between mb-2">
							<span class="text-sm font-medium text-foreground flex items-center gap-2">
								<span class="w-5 h-5 bg-emerald-500/15 rounded flex items-center justify-center">
									<svg class="w-3 h-3 text-emerald-400" viewBox="0 0 24 24" fill="currentColor">
										<path d="M22.28 9.82a5.98 5.98 0 00-.52-4.91 6.05 6.05 0 00-6.51-2.9A6.07 6.07 0 004.98 4.18a5.98 5.98 0 00-4 2.9 6.05 6.05 0 00.74 7.1 5.98 5.98 0 00.51 4.91 6.05 6.05 0 006.51 2.9A5.98 5.98 0 0013.26 24a6.06 6.06 0 005.77-4.21 5.99 5.99 0 004-2.9 6.06 6.06 0 00-.75-7.07z"/>
									</svg>
								</span>
								OpenAI
							</span>
							{#if openaiApiKeyMasked}
								<span class="text-xs text-success">✓</span>
							{/if}
						</div>
						{#if openaiApiKeyMasked}
							<code class="text-xs text-muted-foreground font-mono block mb-2">{openaiApiKeyMasked}</code>
						{/if}
						<div class="flex gap-2">
							<input type="password" bind:value={openaiApiKey} placeholder={openaiApiKeyMasked ? "New key..." : "sk-..."} class="flex-1 px-2 py-1.5 bg-input border border-border rounded-lg text-xs font-mono" />
							<button onclick={saveOpenaiApiKey} disabled={savingOpenaiKey || !openaiApiKey.trim()} class="px-3 py-1.5 bg-primary text-primary-foreground rounded-lg text-xs font-medium disabled:opacity-50">
								{savingOpenaiKey ? '...' : 'Save'}
							</button>
						</div>
					</div>

					<!-- Google Gemini -->
					<div class="p-4 bg-muted/30 rounded-xl border border-border">
						<div class="flex items-center justify-between mb-2">
							<span class="text-sm font-medium text-foreground flex items-center gap-2">
								<span class="w-5 h-5 bg-blue-500/15 rounded flex items-center justify-center">
									<svg class="w-3 h-3 text-blue-400" viewBox="0 0 24 24" fill="currentColor">
										<path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
									</svg>
								</span>
								Google Gemini
							</span>
							{#if imageApiKeyMasked}
								<span class="text-xs text-success">✓</span>
							{/if}
						</div>
						{#if imageApiKeyMasked}
							<code class="text-xs text-muted-foreground font-mono block mb-2">{imageApiKeyMasked}</code>
						{/if}
						<div class="flex gap-2">
							<input type="password" bind:value={imageApiKey} placeholder={imageApiKeyMasked ? "New key..." : "AIza..."} class="flex-1 px-2 py-1.5 bg-input border border-border rounded-lg text-xs font-mono" />
							<button onclick={saveGeminiApiKey} disabled={savingImageConfig || (!imageApiKey.trim() && !imageApiKeyMasked)} class="px-3 py-1.5 bg-primary text-primary-foreground rounded-lg text-xs font-medium disabled:opacity-50">
								{savingImageConfig ? '...' : 'Save'}
							</button>
						</div>
					</div>
				</div>
			</div>

			<!-- AI Models Section -->
			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-3">Default AI Models</h3>

				<!-- Image Generation -->
				<div class="mb-4">
					<div class="flex items-center gap-2 mb-2">
						<span class="w-5 h-5 bg-purple-500/15 rounded flex items-center justify-center">
							<svg class="w-3 h-3 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
							</svg>
						</span>
						<span class="text-xs font-medium text-foreground">Image Generation</span>
					</div>
					<div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
						{#each imageModels as model}
							<button
								onclick={() => updateImageModel(model.id)}
								disabled={!model.available || updatingImageModel}
								class="p-2.5 rounded-lg border text-left transition-all {model.id === imageModel ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
							>
								<div class="text-xs font-medium text-foreground truncate">{model.name}</div>
								<div class="text-[10px] text-primary">${model.price_per_image}/img</div>
							</button>
						{/each}
					</div>
				</div>

				<!-- Video Generation -->
				<div class="mb-4">
					<div class="flex items-center gap-2 mb-2">
						<span class="w-5 h-5 bg-pink-500/15 rounded flex items-center justify-center">
							<svg class="w-3 h-3 text-pink-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
							</svg>
						</span>
						<span class="text-xs font-medium text-foreground">Video Generation</span>
					</div>
					<div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
						{#each videoModels as model}
							<button
								onclick={() => updateVideoModel(model.id)}
								disabled={!model.available || savingVideoModel}
								class="p-2.5 rounded-lg border text-left transition-all {model.id === videoModel ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
							>
								<div class="text-xs font-medium text-foreground truncate">{model.name}</div>
								<div class="text-[10px] text-primary">${model.price_per_second}/sec</div>
							</button>
						{/each}
					</div>
				</div>

				<!-- Audio Models -->
				<div class="grid sm:grid-cols-2 gap-4">
					<!-- STT -->
					<div>
						<div class="flex items-center gap-2 mb-2">
							<span class="w-5 h-5 bg-amber-500/15 rounded flex items-center justify-center">
								<svg class="w-3 h-3 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
								</svg>
							</span>
							<span class="text-xs font-medium text-foreground">Speech-to-Text</span>
						</div>
						<div class="flex flex-wrap gap-2">
							{#each sttModels as model}
								<button
									onclick={() => updateSttModel(model.id)}
									disabled={!model.available || savingSttModel}
									class="px-3 py-1.5 rounded-lg border text-xs transition-all {model.id === currentSttModel ? 'border-primary bg-primary/10 text-foreground' : 'border-border text-muted-foreground hover:border-primary/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
								>
									{model.name}
								</button>
							{/each}
						</div>
					</div>

					<!-- TTS -->
					<div>
						<div class="flex items-center gap-2 mb-2">
							<span class="w-5 h-5 bg-cyan-500/15 rounded flex items-center justify-center">
								<svg class="w-3 h-3 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
								</svg>
							</span>
							<span class="text-xs font-medium text-foreground">Text-to-Speech</span>
						</div>
						<div class="flex flex-wrap gap-2">
							{#each ttsModels as model}
								<button
									onclick={() => updateTtsModel(model.id)}
									disabled={!model.available || savingTtsModel}
									class="px-3 py-1.5 rounded-lg border text-xs transition-all {model.id === currentTtsModel ? 'border-primary bg-primary/10 text-foreground' : 'border-border text-muted-foreground hover:border-primary/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
								>
									{model.name}
								</button>
							{/each}
						</div>
					</div>
				</div>
			</div>

			<!-- Security Section -->
			<div>
				<h3 class="text-sm font-semibold text-primary uppercase tracking-wide mb-3">Security</h3>
				<div class="p-4 bg-muted/30 rounded-xl border border-border">
					<h4 class="text-sm font-medium text-foreground mb-3">Change Password</h4>
					<div class="space-y-3 max-w-sm">
						<input type="password" bind:value={currentPassword} placeholder="Current password" class="w-full px-3 py-2 bg-input border border-border rounded-lg text-sm" />
						<input type="password" bind:value={newPassword} placeholder="New password (min 8 chars)" class="w-full px-3 py-2 bg-input border border-border rounded-lg text-sm" />
						<input type="password" bind:value={confirmPassword} placeholder="Confirm new password" class="w-full px-3 py-2 bg-input border border-border rounded-lg text-sm" />
						{#if passwordChangeError}
							<p class="text-xs text-destructive">{passwordChangeError}</p>
						{/if}
						{#if passwordChangeSuccess}
							<p class="text-xs text-success">{passwordChangeSuccess}</p>
						{/if}
						<button
							onclick={handlePasswordChange}
							disabled={changingPassword}
							class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:opacity-90 disabled:opacity-50"
						>
							{changingPassword ? 'Changing...' : 'Change Password'}
						</button>
					</div>
				</div>
			</div>
		</div>
	{/if}
</UniversalModal>
