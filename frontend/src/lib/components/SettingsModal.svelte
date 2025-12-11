<script lang="ts">
	/**
	 * SettingsModal - A tabbed modal for all application settings
	 *
	 * Converted from the full-page settings to a modal interface
	 * with tabs: General, Security, Authentication, API Users, Integrations
	 */
	import { onMount } from 'svelte';
	import { auth, username, isAdmin, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import { api } from '$lib/api/client';
	import type { ApiUser, ApiUserWithKey, Profile, WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import type { Project } from '$lib/stores/chat';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig, changePassword } from '$lib/api/auth';

	interface Props {
		open: boolean;
		onClose: () => void;
	}

	let { open, onClose }: Props = $props();

	// Tab management
	type SettingsTab = 'general' | 'security' | 'authentication' | 'api-users' | 'integrations';
	let activeTab: SettingsTab = $state('general');

	// Data state
	let apiUsers: ApiUser[] = $state([]);
	let profiles: Profile[] = $state([]);
	let projects: Project[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// Form state for API Users
	let showCreateForm = $state(false);
	let editingUser: ApiUser | null = $state(null);
	let newlyCreatedKey: string | null = $state(null);
	let regeneratedKey: string | null = $state(null);

	// Workspace state
	let workspaceConfig: WorkspaceConfig | null = $state(null);
	let workspacePath = $state('');
	let workspaceValidation: WorkspaceValidation | null = $state(null);
	let validatingWorkspace = $state(false);
	let savingWorkspace = $state(false);
	let workspaceError = $state('');
	let workspaceSuccess = $state('');

	// Auth state
	let githubToken = $state('');
	let githubLoginLoading = $state(false);
	let claudeLoginLoading = $state(false);
	let claudeOAuthUrl: string | null = $state(null);
	let claudeAuthCode = $state('');
	let claudeCompletingLogin = $state(false);
	let githubUser: string | null = $state(null);

	// Integration settings - OpenAI
	let openaiApiKey = $state('');
	let openaiApiKeyMasked = $state('');
	let savingOpenaiKey = $state(false);
	let openaiKeySuccess = $state('');
	let openaiKeyError = $state('');

	// Image generation settings
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
	interface ImageProvider {
		id: string;
		name: string;
		description: string;
		available: boolean;
		is_current: boolean;
	}
	let imageModels: ImageModel[] = $state([]);
	let imageProviders: ImageProvider[] = $state([]);
	let imageProvider = $state('');
	let imageModel = $state('');
	let imageApiKey = $state('');
	let imageApiKeyMasked = $state('');
	let savingImageConfig = $state(false);
	let imageConfigSuccess = $state('');
	let imageConfigError = $state('');
	let selectedImageModel = $state('');
	let updatingImageModel = $state(false);

	// Video generation settings
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
	interface VideoProvider {
		id: string;
		name: string;
		description: string;
		available: boolean;
		is_current: boolean;
	}
	let videoModels: VideoModel[] = $state([]);
	let videoProviders: VideoProvider[] = $state([]);
	let videoModel = $state('');
	let videoProvider = $state('');
	let selectedVideoModel = $state('');
	let savingVideoModel = $state(false);
	let videoConfigSuccess = $state('');
	let videoConfigError = $state('');

	// Audio settings
	interface AudioModel {
		id: string;
		name: string;
		description: string;
		price_display: string;
		available: boolean;
		is_current: boolean;
	}
	let ttsModels: AudioModel[] = $state([]);
	let sttModels: AudioModel[] = $state([]);
	let selectedTtsModel = $state('');
	let selectedSttModel = $state('');
	let currentTtsModel = $state('');
	let currentSttModel = $state('');
	let savingTtsModel = $state(false);
	let savingSttModel = $state(false);
	let audioConfigSuccess = $state('');
	let audioConfigError = $state('');

	// Password change state
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let changingPassword = $state(false);
	let passwordChangeError = $state('');
	let passwordChangeSuccess = $state('');

	// API User form data
	let formData = $state({
		name: '',
		description: '',
		project_id: '',
		profile_id: '',
		web_login_allowed: true
	});

	// Tab definitions
	const tabs: { id: SettingsTab; label: string; icon: string }[] = [
		{ id: 'general', label: 'General', icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z' },
		{ id: 'security', label: 'Security', icon: 'M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z' },
		{ id: 'authentication', label: 'Authentication', icon: 'M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z' },
		{ id: 'api-users', label: 'API Users', icon: 'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z' },
		{ id: 'integrations', label: 'Integrations', icon: 'M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z' }
	];

	// Load data when modal opens
	$effect(() => {
		if (open && $isAdmin) {
			loadAllData();
		}
	});

	async function loadAllData() {
		await Promise.all([
			loadData(),
			loadAuthStatus(),
			loadWorkspaceConfig(),
			loadIntegrationSettings()
		]);
	}

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

	async function loadWorkspaceConfig() {
		try {
			workspaceConfig = await getWorkspaceConfig();
			workspacePath = workspaceConfig.workspace_path;
		} catch (e) {
			console.error('Failed to load workspace config:', e);
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
			imageProvider = settings.image_provider || '';
			imageModel = settings.image_model || '';
			imageApiKeyMasked = settings.image_api_key_masked || '';
			selectedImageModel = settings.image_model || '';
			videoProvider = settings.video_provider || '';
			videoModel = settings.video_model || '';
			selectedVideoModel = settings.video_model || '';
			currentTtsModel = settings.tts_model || 'gpt-4o-mini-tts';
			currentSttModel = settings.stt_model || 'whisper-1';
			selectedTtsModel = currentTtsModel;
			selectedSttModel = currentSttModel;
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}

		// Load image models
		try {
			const imageResponse = await api.get<{
				models: ImageModel[];
				providers: ImageProvider[];
				current_provider: string | null;
				current_model: string | null;
			}>('/settings/integrations/image/models');
			imageModels = imageResponse.models;
			imageProviders = imageResponse.providers;
			if (!selectedImageModel && imageResponse.current_model) {
				selectedImageModel = imageResponse.current_model;
			} else if (!selectedImageModel && imageModels.length > 0) {
				const availableModel = imageModels.find(m => m.available);
				selectedImageModel = availableModel?.id || imageModels[0].id;
			}
		} catch (e) {
			console.error('Failed to load image models:', e);
		}

		// Load video models
		try {
			const videoResponse = await api.get<{
				models: VideoModel[];
				providers: VideoProvider[];
				current_provider: string | null;
				current_model: string | null;
			}>('/settings/integrations/video/models');
			videoModels = videoResponse.models;
			videoProviders = videoResponse.providers;
			if (!selectedVideoModel && videoResponse.current_model) {
				selectedVideoModel = videoResponse.current_model;
			} else if (!selectedVideoModel && videoModels.length > 0) {
				const availableModel = videoModels.find(m => m.available);
				selectedVideoModel = availableModel?.id || videoModels[0].id;
			}
		} catch (e) {
			console.error('Failed to load video models:', e);
		}

		// Load audio models
		try {
			const audioResponse = await api.get<{
				tts_models: AudioModel[];
				stt_models: AudioModel[];
				current_tts: string;
				current_stt: string;
				openai_configured: boolean;
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

	// OpenAI API Key functions
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

	// Image config functions
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
				api_key: imageApiKey || imageApiKeyMasked
			});
			imageProvider = result.provider;
			imageModel = result.model;
			imageApiKeyMasked = result.masked_key;
			imageApiKey = '';
			imageConfigSuccess = 'Image generation configured successfully';
		} catch (e: any) {
			imageConfigError = e.detail || 'Failed to save configuration';
		} finally {
			savingImageConfig = false;
		}
	}

	async function updateImageModel() {
		if (!selectedImageModel || selectedImageModel === imageModel) return;
		updatingImageModel = true;
		imageConfigError = '';
		imageConfigSuccess = '';
		try {
			const result = await api.patch<{success: boolean, provider: string, model: string}>('/settings/integrations/image/model', {
				model: selectedImageModel
			});
			imageModel = result.model;
			imageProvider = result.provider;
			imageConfigSuccess = 'Image model updated successfully';
			await loadIntegrationSettings();
		} catch (e: any) {
			imageConfigError = e.detail || 'Failed to update image model';
		} finally {
			updatingImageModel = false;
		}
	}

	// Video model functions
	async function saveVideoModel() {
		if (!selectedVideoModel || selectedVideoModel === videoModel) return;
		savingVideoModel = true;
		videoConfigError = '';
		videoConfigSuccess = '';
		try {
			const result = await api.patch<{success: boolean, provider: string, model: string}>('/settings/integrations/video/model', {
				model: selectedVideoModel
			});
			videoModel = result.model;
			videoProvider = result.provider;
			videoConfigSuccess = 'Video model updated successfully';
			await loadIntegrationSettings();
		} catch (e: any) {
			videoConfigError = e.detail || 'Failed to update video model';
		} finally {
			savingVideoModel = false;
		}
	}

	// Audio model functions
	async function saveTtsModel() {
		if (!selectedTtsModel || selectedTtsModel === currentTtsModel) return;
		savingTtsModel = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean, model: string}>('/settings/integrations/audio/tts', {
				model: selectedTtsModel
			});
			currentTtsModel = selectedTtsModel;
			audioConfigSuccess = 'TTS model updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: any) {
			audioConfigError = e.detail || 'Failed to update TTS model';
		} finally {
			savingTtsModel = false;
		}
	}

	async function saveSttModel() {
		if (!selectedSttModel || selectedSttModel === currentSttModel) return;
		savingSttModel = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean, model: string}>('/settings/integrations/audio/stt', {
				model: selectedSttModel
			});
			currentSttModel = selectedSttModel;
			audioConfigSuccess = 'STT model updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: any) {
			audioConfigError = e.detail || 'Failed to update STT model';
		} finally {
			savingSttModel = false;
		}
	}

	// Workspace functions
	let workspaceValidateTimeout: ReturnType<typeof setTimeout>;

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

	async function handleClaudeReconnect() {
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
			passwordChangeSuccess = 'Password changed successfully. All encrypted data has been re-encrypted.';
			currentPassword = '';
			newPassword = '';
			confirmPassword = '';
		} catch (e: any) {
			passwordChangeError = e.detail || 'Failed to change password';
		} finally {
			changingPassword = false;
		}
	}

	// API User CRUD functions
	function resetForm() {
		formData = {
			name: '',
			description: '',
			project_id: '',
			profile_id: '',
			web_login_allowed: true
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
			profile_id: user.profile_id || '',
			web_login_allowed: user.web_login_allowed ?? true
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
					profile_id: formData.profile_id || null,
					web_login_allowed: formData.web_login_allowed
				});
			} else {
				const result = await api.post<ApiUserWithKey>('/api-users', {
					name: formData.name,
					description: formData.description || null,
					project_id: formData.project_id || null,
					profile_id: formData.profile_id || null,
					web_login_allowed: formData.web_login_allowed
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
			await api.put(`/api-users/${user.id}`, { is_active: !user.is_active });
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to update user';
		}
	}

	async function regenerateKey(userId: string) {
		if (!confirm('Are you sure you want to regenerate this API key? The old key will stop working immediately.')) return;
		try {
			const result = await api.post<ApiUserWithKey>(`/api-users/${userId}/regenerate-key`);
			regeneratedKey = result.api_key;
			alert(`New API Key (copy now - won't be shown again):\n\n${result.api_key}`);
			regeneratedKey = null;
		} catch (e: any) {
			error = e.detail || 'Failed to regenerate key';
		}
	}

	async function deleteUser(userId: string) {
		if (!confirm('Are you sure you want to delete this API user? This cannot be undone.')) return;
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

	// Handle escape key
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			if (showCreateForm) {
				resetForm();
			} else {
				onClose();
			}
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<!-- Backdrop -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center"
		role="dialog"
		aria-modal="true"
		aria-labelledby="settings-modal-title"
	>
		<button
			class="absolute inset-0 bg-black/60 backdrop-blur-sm"
			onclick={onClose}
			aria-label="Close settings"
		></button>

		<!-- Modal Container -->
		<div
			class="
				relative w-full max-w-4xl mx-auto
				bg-card border border-border rounded-2xl shadow-2xl
				flex flex-col overflow-hidden
				max-h-[90vh] sm:max-h-[85vh]
				max-sm:fixed max-sm:inset-3 max-sm:bottom-[4.5rem] max-sm:max-w-none max-sm:max-h-none max-sm:rounded-2xl
				transform transition-all duration-200 ease-out
				animate-modal-in
			"
			onclick={(e) => e.stopPropagation()}
		>
			<!-- Header -->
			<header class="shrink-0 px-6 py-4 border-b border-border bg-card">
				<div class="flex items-center justify-between">
					<div class="flex items-center gap-3">
						<div class="w-10 h-10 rounded-xl bg-primary/15 flex items-center justify-center">
							<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
							</svg>
						</div>
						<h2 id="settings-modal-title" class="text-xl font-semibold text-foreground">Settings</h2>
					</div>
					<button
						onclick={onClose}
						class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
						aria-label="Close"
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<!-- Tab Navigation -->
				<nav class="flex gap-1 mt-4 -mb-4 px-0 overflow-x-auto scrollbar-none" aria-label="Settings tabs">
					{#each tabs as tab}
						<button
							onclick={() => activeTab = tab.id}
							class="
								px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all whitespace-nowrap
								border-b-2 -mb-[1px] flex items-center gap-2
								{activeTab === tab.id
									? 'text-primary border-primary bg-primary/5'
									: 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}
							"
							aria-current={activeTab === tab.id ? 'page' : undefined}
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d={tab.icon} />
							</svg>
							<span class="hidden sm:inline">{tab.label}</span>
						</button>
					{/each}
				</nav>
			</header>

			<!-- Body -->
			<div class="flex-1 overflow-y-auto p-6">
				{#if error}
					<div class="bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 rounded-lg mb-4">
						{error}
						<button onclick={() => error = ''} class="ml-2 hover:opacity-70">&times;</button>
					</div>
				{/if}

				<!-- General Tab -->
				{#if activeTab === 'general'}
					<div class="space-y-6">
						<div>
							<h3 class="text-lg font-semibold text-foreground mb-1">General Settings</h3>
							<p class="text-sm text-muted-foreground">Configure your workspace and application preferences.</p>
						</div>

						<!-- Workspace Configuration -->
						<section class="bg-muted/30 rounded-xl p-5">
							<h4 class="text-base font-semibold text-foreground mb-4 flex items-center gap-2">
								<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
								</svg>
								Workspace Folder
							</h4>

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
												oninput={handleWorkspaceInput}
												class="input font-mono text-sm flex-1"
												placeholder="Enter workspace path"
											/>
											<button
												onclick={saveWorkspace}
												disabled={savingWorkspace || validatingWorkspace || !workspaceValidation?.valid || workspacePath === workspaceConfig?.workspace_path}
												class="btn btn-primary whitespace-nowrap"
											>
												{#if savingWorkspace}
													<span class="inline-block animate-spin mr-2">&#9696;</span>
												{/if}
												Save
											</button>
										</div>
										<p class="text-muted-foreground text-xs mt-1.5">Full path to your projects folder</p>
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
												<span>{workspaceValidation.exists ? 'Directory exists and is writable' : 'Directory will be created'}</span>
											</div>
										{:else}
											<div class="bg-destructive/10 border border-destructive/30 text-destructive px-3 py-2 rounded-lg text-sm">
												{workspaceError || 'Invalid path'}
											</div>
										{/if}
									{/if}

									{#if workspaceSuccess}
										<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm flex items-center gap-2">
											<span>✓</span>
											<span>{workspaceSuccess}</span>
										</div>
									{/if}
								</div>
							{:else}
								<div class="flex items-start gap-3">
									<span class="text-blue-400 text-xl">🐳</span>
									<div>
										<h5 class="text-blue-300 font-medium mb-1">Docker Mode</h5>
										<p class="text-muted-foreground text-sm mb-2">
											AI Hub is running in Docker. The workspace is managed by Docker volume mounts.
										</p>
										<p class="text-muted-foreground/70 text-xs">
											Current workspace: <code class="bg-muted px-2 py-0.5 rounded">{workspaceConfig?.workspace_path || '/workspace'}</code>
										</p>
									</div>
								</div>
							{/if}
						</section>
					</div>
				{/if}

				<!-- Security Tab -->
				{#if activeTab === 'security'}
					<div class="space-y-6">
						<div>
							<h3 class="text-lg font-semibold text-foreground mb-1">Security Settings</h3>
							<p class="text-sm text-muted-foreground">Manage your account security and encryption settings.</p>
						</div>

						<!-- Change Password -->
						<section class="bg-muted/30 rounded-xl p-5">
							<div class="flex items-center gap-3 mb-4">
								<div class="w-10 h-10 bg-primary/15 rounded-lg flex items-center justify-center">
									<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
									</svg>
								</div>
								<div>
									<h4 class="font-semibold text-foreground">Change Password</h4>
									<p class="text-xs text-muted-foreground">Update your admin password. This will re-encrypt all stored API keys.</p>
								</div>
							</div>

							<div class="space-y-4 max-w-md">
								<div>
									<label for="currentPassword" class="block text-sm font-medium text-foreground mb-1.5">Current Password</label>
									<input type="password" id="currentPassword" bind:value={currentPassword} class="input" placeholder="Enter current password" autocomplete="current-password" />
								</div>
								<div>
									<label for="newPassword" class="block text-sm font-medium text-foreground mb-1.5">New Password</label>
									<input type="password" id="newPassword" bind:value={newPassword} class="input" placeholder="Enter new password (min 8 characters)" autocomplete="new-password" />
								</div>
								<div>
									<label for="confirmPassword" class="block text-sm font-medium text-foreground mb-1.5">Confirm New Password</label>
									<input type="password" id="confirmPassword" bind:value={confirmPassword} class="input" placeholder="Confirm new password" autocomplete="new-password" />
								</div>

								{#if passwordChangeError}
									<div class="bg-destructive/10 border border-destructive/30 text-destructive px-3 py-2 rounded-lg text-sm">{passwordChangeError}</div>
								{/if}
								{#if passwordChangeSuccess}
									<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm">{passwordChangeSuccess}</div>
								{/if}

								<button onclick={handlePasswordChange} class="btn btn-primary" disabled={changingPassword}>
									{#if changingPassword}
										<span class="inline-block animate-spin mr-2">&#9696;</span>
										Changing Password...
									{:else}
										Change Password
									{/if}
								</button>
							</div>
						</section>

						<!-- Encryption Info -->
						<section class="bg-muted/30 rounded-xl p-5">
							<div class="flex items-center gap-3">
								<div class="w-10 h-10 bg-success/15 rounded-lg flex items-center justify-center">
									<svg class="w-5 h-5 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
									</svg>
								</div>
								<div>
									<h4 class="font-semibold text-foreground">Encryption</h4>
									<p class="text-xs text-muted-foreground">Your API keys are encrypted at rest using your admin password.</p>
								</div>
							</div>
						</section>
					</div>
				{/if}

				<!-- Authentication Tab -->
				{#if activeTab === 'authentication'}
					<div class="space-y-4">
						<div>
							<h3 class="text-lg font-semibold text-foreground mb-1">Service Authentication</h3>
							<p class="text-sm text-muted-foreground">Connect services to enable AI features and repository management.</p>
						</div>

						<div class="grid gap-4 sm:grid-cols-2">
							<!-- Claude Code -->
							<div class="bg-muted/30 rounded-xl p-4">
								<div class="flex items-center gap-3 mb-3">
									<div class="w-9 h-9 bg-orange-500/15 rounded-lg flex items-center justify-center shrink-0">
										<svg class="w-4 h-4 text-orange-400" viewBox="0 0 24 24" fill="currentColor">
											<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center justify-between gap-2">
											<span class="font-semibold text-foreground text-sm">Claude Code</span>
											{#if $claudeAuthenticated}
												<span class="text-[10px] px-2 py-0.5 rounded-full bg-success/15 text-success font-medium shrink-0">Connected</span>
											{:else}
												<span class="text-[10px] px-2 py-0.5 rounded-full bg-warning/15 text-warning font-medium shrink-0">Not Connected</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground truncate">AI-powered code assistance</p>
									</div>
								</div>

								{#if $claudeAuthenticated}
									<div class="flex gap-2">
										<button onclick={handleClaudeReconnect} disabled={claudeLoginLoading} class="btn btn-primary text-xs py-1.5 flex-1">
											{#if claudeLoginLoading}
												<span class="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full mr-1"></span>
											{/if}
											Reconnect
										</button>
										<button onclick={handleClaudeLogout} class="btn btn-secondary text-xs py-1.5 flex-1">Disconnect</button>
									</div>
								{:else if claudeOAuthUrl}
									<div class="space-y-2">
										<a href={claudeOAuthUrl} target="_blank" rel="noopener noreferrer" class="btn btn-primary text-xs py-1.5 w-full flex items-center justify-center gap-1.5">
											<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
											</svg>
											1. Open Login
										</a>
										<div class="flex gap-2">
											<input type="text" bind:value={claudeAuthCode} placeholder="2. Paste code" class="input text-xs py-1.5 flex-1" />
											<button onclick={completeClaudeLogin} disabled={claudeCompletingLogin || !claudeAuthCode.trim()} class="btn btn-primary text-xs py-1.5 px-3">
												{#if claudeCompletingLogin}
													<span class="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full"></span>
												{:else}
													OK
												{/if}
											</button>
										</div>
										<button onclick={cancelClaudeLogin} class="text-[10px] text-muted-foreground hover:text-foreground w-full text-center">Cancel</button>
									</div>
								{:else}
									<button onclick={() => handleClaudeLogin()} disabled={claudeLoginLoading} class="btn btn-primary text-xs py-1.5 w-full">
										{#if claudeLoginLoading}
											<span class="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full mr-1"></span>
										{/if}
										Connect
									</button>
								{/if}
							</div>

							<!-- GitHub -->
							<div class="bg-muted/30 rounded-xl p-4">
								<div class="flex items-center gap-3 mb-3">
									<div class="w-9 h-9 bg-white/10 rounded-lg flex items-center justify-center shrink-0">
										<svg class="w-4 h-4 text-white" viewBox="0 0 24 24" fill="currentColor">
											<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center justify-between gap-2">
											<span class="font-semibold text-foreground text-sm">GitHub</span>
											{#if $githubAuthenticated}
												<span class="text-[10px] px-2 py-0.5 rounded-full bg-success/15 text-success font-medium shrink-0">Connected</span>
											{:else}
												<span class="text-[10px] px-2 py-0.5 rounded-full bg-warning/15 text-warning font-medium shrink-0">Not Connected</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground truncate">
											{#if $githubAuthenticated && githubUser}{githubUser}{:else}Repository management{/if}
										</p>
									</div>
								</div>

								{#if $githubAuthenticated}
									<button onclick={handleGitHubLogout} class="btn btn-secondary text-xs py-1.5 w-full">Disconnect</button>
								{:else}
									<div class="space-y-2">
										<input type="password" bind:value={githubToken} placeholder="Personal Access Token" class="input text-xs py-1.5" />
										<button onclick={handleGitHubLogin} disabled={githubLoginLoading} class="btn btn-primary text-xs py-1.5 w-full">
											{#if githubLoginLoading}
												<span class="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full mr-1"></span>
											{/if}
											Connect
										</button>
										<p class="text-[10px] text-muted-foreground text-center">
											<a href="https://github.com/settings/tokens/new?scopes=repo,read:org,gist,workflow" target="_blank" rel="noopener noreferrer" class="text-primary hover:underline">Create token</a> (repo, read:org, gist, workflow)
										</p>
									</div>
								{/if}
							</div>
						</div>
					</div>
				{/if}

				<!-- API Users Tab -->
				{#if activeTab === 'api-users'}
					<div class="space-y-4">
						<div class="flex items-center justify-between">
							<div>
								<h3 class="text-lg font-semibold text-foreground mb-1">API Users</h3>
								<p class="text-sm text-muted-foreground">Create API users for programmatic access.</p>
							</div>
							<button onclick={openCreateForm} class="btn btn-primary flex items-center gap-2 text-sm">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
								</svg>
								<span class="hidden sm:inline">Create</span>
							</button>
						</div>

						{#if loading}
							<div class="text-center py-8">
								<div class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto"></div>
							</div>
						{:else if apiUsers.length === 0}
							<div class="bg-muted/30 rounded-xl p-8 text-center">
								<div class="w-12 h-12 bg-muted rounded-full flex items-center justify-center mx-auto mb-4">
									<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
									</svg>
								</div>
								<p class="text-foreground font-medium mb-1">No API users yet</p>
								<p class="text-sm text-muted-foreground">Create an API user to allow external applications to access AI Hub.</p>
							</div>
						{:else}
							<div class="space-y-2">
								{#each apiUsers as user}
									<div class="bg-muted/30 rounded-xl p-4">
										<div class="flex items-start justify-between">
											<div class="flex-1 min-w-0">
												<div class="flex items-center gap-2 mb-1 flex-wrap">
													<span class="font-medium text-foreground">{user.name}</span>
													{#if user.username}
														<span class="text-xs px-2 py-0.5 rounded-full bg-primary/15 text-primary">@{user.username}</span>
													{/if}
													<span class="text-xs px-2 py-0.5 rounded-full {user.is_active ? 'bg-success/15 text-success' : 'bg-destructive/15 text-destructive'}">
														{user.is_active ? 'Active' : 'Inactive'}
													</span>
												</div>
												{#if user.description}
													<p class="text-sm text-muted-foreground mb-2">{user.description}</p>
												{/if}
												<div class="flex flex-wrap gap-3 text-xs text-muted-foreground">
													<span>Profile: <span class="text-foreground/80">{profiles.find(p => p.id === user.profile_id)?.name || 'Any'}</span></span>
													<span>Last used: {formatDate(user.last_used_at)}</span>
												</div>
											</div>
											<div class="flex items-center gap-1 ml-3">
												<button onclick={() => openEditForm(user)} class="p-1.5 text-muted-foreground hover:text-foreground rounded-lg hover:bg-accent transition-colors" title="Edit">
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
													</svg>
												</button>
												<button onclick={() => regenerateKey(user.id)} class="p-1.5 text-muted-foreground hover:text-warning rounded-lg hover:bg-accent transition-colors" title="Regenerate Key">
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
													</svg>
												</button>
												<button onclick={() => toggleActive(user)} class="p-1.5 text-muted-foreground hover:text-primary rounded-lg hover:bg-accent transition-colors" title={user.is_active ? 'Deactivate' : 'Activate'}>
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														{#if user.is_active}
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
														{:else}
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
														{/if}
													</svg>
												</button>
												<button onclick={() => deleteUser(user.id)} class="p-1.5 text-muted-foreground hover:text-destructive rounded-lg hover:bg-accent transition-colors" title="Delete">
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

						<!-- API Usage Info -->
						<section class="bg-muted/30 rounded-xl p-4 mt-4">
							<h4 class="text-sm font-semibold text-foreground mb-2">API Usage</h4>
							<p class="text-xs text-muted-foreground mb-2">Use the API key in the Authorization header:</p>
							<pre class="bg-muted p-3 rounded-lg text-xs overflow-x-auto"><code class="text-foreground/90">curl -H "Authorization: Bearer aih_your_key" ...</code></pre>
						</section>
					</div>
				{/if}

				<!-- Integrations Tab -->
				{#if activeTab === 'integrations'}
					<div class="space-y-4">
						<div>
							<h3 class="text-lg font-semibold text-foreground mb-1">Integrations</h3>
							<p class="text-sm text-muted-foreground">Configure API keys and select default models.</p>
						</div>

						{#if audioConfigSuccess || imageConfigSuccess || videoConfigSuccess}
							<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-xs flex items-center gap-2">
								<span>✓</span>
								<span>{audioConfigSuccess || imageConfigSuccess || videoConfigSuccess}</span>
							</div>
						{/if}

						<!-- API Keys -->
						<section class="bg-muted/30 rounded-xl p-4">
							<h4 class="text-sm font-semibold text-foreground mb-3 flex items-center gap-2">
								<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
								</svg>
								API Keys
							</h4>

							<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
								<!-- OpenAI -->
								<div class="p-3 bg-card rounded-lg border border-border">
									<div class="flex items-center justify-between mb-2">
										<span class="text-xs font-medium text-foreground flex items-center gap-1.5">
											<span class="w-5 h-5 bg-emerald-500/15 rounded flex items-center justify-center">
												<svg class="w-3 h-3 text-emerald-400" viewBox="0 0 24 24" fill="currentColor">
													<path d="M22.28 9.82a5.98 5.98 0 00-.52-4.91 6.05 6.05 0 00-6.51-2.9A6.07 6.07 0 004.98 4.18a5.98 5.98 0 00-4 2.9 6.05 6.05 0 00.74 7.1 5.98 5.98 0 00.51 4.91 6.05 6.05 0 006.51 2.9A5.98 5.98 0 0013.26 24a6.06 6.06 0 005.77-4.21 5.99 5.99 0 004-2.9 6.06 6.06 0 00-.75-7.07z"/>
												</svg>
											</span>
											OpenAI
										</span>
										{#if openaiApiKeyMasked}
											<span class="text-[9px] px-1.5 py-0.5 rounded bg-success/15 text-success">✓</span>
										{/if}
									</div>
									{#if openaiApiKeyMasked}
										<code class="text-[10px] text-muted-foreground font-mono">{openaiApiKeyMasked}</code>
									{/if}
									<div class="flex gap-1.5 mt-2">
										<input type="password" bind:value={openaiApiKey} placeholder={openaiApiKeyMasked ? "New key..." : "sk-..."} class="input text-[10px] py-1 px-2 font-mono flex-1 min-w-0" />
										<button onclick={saveOpenaiApiKey} disabled={savingOpenaiKey || !openaiApiKey.trim()} class="btn btn-primary text-[10px] py-1 px-2">
											{savingOpenaiKey ? '...' : 'Save'}
										</button>
									</div>
									<p class="text-[9px] text-muted-foreground mt-1.5">
										<a href="https://platform.openai.com/api-keys" target="_blank" class="text-primary hover:underline">Get key</a> · TTS, STT, GPT Image, Sora
									</p>
								</div>

								<!-- Google Gemini -->
								<div class="p-3 bg-card rounded-lg border border-border">
									<div class="flex items-center justify-between mb-2">
										<span class="text-xs font-medium text-foreground flex items-center gap-1.5">
											<span class="w-5 h-5 bg-blue-500/15 rounded flex items-center justify-center">
												<svg class="w-3 h-3 text-blue-400" viewBox="0 0 24 24" fill="currentColor">
													<path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
												</svg>
											</span>
											Google Gemini
										</span>
										{#if imageApiKeyMasked}
											<span class="text-[9px] px-1.5 py-0.5 rounded bg-success/15 text-success">✓</span>
										{/if}
									</div>
									{#if imageApiKeyMasked}
										<code class="text-[10px] text-muted-foreground font-mono">{imageApiKeyMasked}</code>
									{/if}
									<div class="flex gap-1.5 mt-2">
										<input type="password" bind:value={imageApiKey} placeholder={imageApiKeyMasked ? "New key..." : "AIza..."} class="input text-[10px] py-1 px-2 font-mono flex-1 min-w-0" />
										<button onclick={saveImageConfig} disabled={savingImageConfig || (!imageApiKey.trim() && !imageApiKeyMasked)} class="btn btn-primary text-[10px] py-1 px-2">
											{savingImageConfig ? '...' : 'Save'}
										</button>
									</div>
									<p class="text-[9px] text-muted-foreground mt-1.5">
										<a href="https://aistudio.google.com/apikey" target="_blank" class="text-primary hover:underline">Get key</a> · Nano Banana, Imagen 4, Veo
									</p>
								</div>
							</div>
						</section>

						<!-- AI Models -->
						<section class="bg-muted/30 rounded-xl p-4">
							<h4 class="text-sm font-semibold text-foreground mb-1 flex items-center gap-2">
								<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
								</svg>
								AI Generation Models
							</h4>
							<p class="text-[10px] text-muted-foreground mb-3">Select default models. Claude can switch providers dynamically.</p>

							<!-- Image Models -->
							<div class="mb-4">
								<div class="flex items-center gap-2 mb-2">
									<span class="w-5 h-5 bg-purple-500/15 rounded flex items-center justify-center">
										<svg class="w-3 h-3 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
										</svg>
									</span>
									<span class="text-xs font-medium text-foreground">Image Generation</span>
								</div>
								<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
									{#each imageModels as model}
										<button
											onclick={() => { selectedImageModel = model.id; updateImageModel(); }}
											disabled={!model.available || updatingImageModel}
											class="h-[68px] p-2 rounded-lg border text-left transition-all flex flex-col justify-between {model.id === imageModel ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50 hover:bg-muted/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
										>
											<div class="text-[10px] font-medium text-foreground leading-tight">{model.name}</div>
											<div>
												<div class="text-[9px] text-primary font-medium">${model.price_per_image}/img</div>
												<div class="text-[8px] text-muted-foreground truncate">{model.provider_name}</div>
											</div>
										</button>
									{/each}
								</div>
							</div>

							<!-- Video Models -->
							<div class="mb-4">
								<div class="flex items-center gap-2 mb-2">
									<span class="w-5 h-5 bg-pink-500/15 rounded flex items-center justify-center">
										<svg class="w-3 h-3 text-pink-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
										</svg>
									</span>
									<span class="text-xs font-medium text-foreground">Video Generation</span>
								</div>
								<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
									{#each videoModels as model}
										<button
											onclick={() => { selectedVideoModel = model.id; saveVideoModel(); }}
											disabled={!model.available || savingVideoModel}
											class="h-[68px] p-2 rounded-lg border text-left transition-all flex flex-col justify-between {model.id === videoModel ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50 hover:bg-muted/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
										>
											<div class="text-[10px] font-medium text-foreground leading-tight">{model.name}</div>
											<div>
												<div class="text-[9px] text-primary font-medium">${model.price_per_second}/sec</div>
												<div class="text-[8px] text-muted-foreground truncate">{model.provider_name}</div>
											</div>
										</button>
									{/each}
								</div>
							</div>

							<!-- STT Models -->
							<div class="mb-4">
								<div class="flex items-center gap-2 mb-2">
									<span class="w-5 h-5 bg-amber-500/15 rounded flex items-center justify-center">
										<svg class="w-3 h-3 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
										</svg>
									</span>
									<span class="text-xs font-medium text-foreground">Speech-to-Text</span>
								</div>
								<div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
									{#each sttModels as model}
										<button
											onclick={() => { selectedSttModel = model.id; saveSttModel(); }}
											disabled={!model.available || savingSttModel}
											class="h-[68px] p-2 rounded-lg border text-left transition-all flex flex-col justify-between {model.id === currentSttModel ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50 hover:bg-muted/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
										>
											<div class="text-[10px] font-medium text-foreground leading-tight">{model.name}</div>
											<div class="text-[9px] text-primary font-medium">{model.price_display}</div>
										</button>
									{/each}
								</div>
							</div>

							<!-- TTS Models -->
							<div>
								<div class="flex items-center gap-2 mb-2">
									<span class="w-5 h-5 bg-cyan-500/15 rounded flex items-center justify-center">
										<svg class="w-3 h-3 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
										</svg>
									</span>
									<span class="text-xs font-medium text-foreground">Text-to-Speech</span>
								</div>
								<div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
									{#each ttsModels as model}
										<button
											onclick={() => { selectedTtsModel = model.id; saveTtsModel(); }}
											disabled={!model.available || savingTtsModel}
											class="h-[68px] p-2 rounded-lg border text-left transition-all flex flex-col justify-between {model.id === currentTtsModel ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50 hover:bg-muted/50'} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
										>
											<div class="text-[10px] font-medium text-foreground leading-tight">{model.name}</div>
											<div class="text-[9px] text-primary font-medium">{model.price_display}</div>
										</button>
									{/each}
								</div>
							</div>
						</section>
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<footer class="shrink-0 px-6 py-4 border-t border-border bg-card/50 flex items-center justify-end">
				<button
					onclick={onClose}
					class="px-6 py-2.5 rounded-xl text-sm font-medium bg-muted text-foreground border border-border hover:bg-accent transition-colors"
				>
					Close
				</button>
			</footer>
		</div>
	</div>

	<!-- Create/Edit API User Modal (nested) -->
	{#if showCreateForm}
		<div class="fixed inset-0 bg-black/60 backdrop-blur-sm z-[60] flex items-center justify-center p-4">
			<div class="bg-card border border-border rounded-2xl w-full max-w-lg shadow-2xl">
				<div class="p-4 border-b border-border flex items-center justify-between">
					<h3 class="text-lg font-bold text-foreground">
						{editingUser ? 'Edit API User' : 'Create API User'}
					</h3>
					<button class="text-muted-foreground hover:text-foreground transition-colors" onclick={resetForm}>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<div class="p-4 space-y-4">
					{#if newlyCreatedKey}
						<div class="bg-success/10 border border-success/30 p-4 rounded-lg">
							<p class="text-success font-medium mb-2">API User created successfully!</p>
							<p class="text-sm text-muted-foreground mb-3">Copy this API key now - it won't be shown again:</p>
							<div class="flex items-center gap-2">
								<code class="flex-1 bg-muted px-3 py-2 rounded-lg text-sm text-foreground break-all font-mono">{newlyCreatedKey}</code>
								<button onclick={() => copyToClipboard(newlyCreatedKey || '')} class="btn btn-secondary shrink-0">Copy</button>
							</div>
						</div>
						<button onclick={resetForm} class="btn btn-primary w-full">Done</button>
					{:else}
						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Name *</label>
							<input bind:value={formData.name} class="input" placeholder="My Application" />
						</div>
						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Description</label>
							<textarea bind:value={formData.description} class="input" rows="2" placeholder="Optional description"></textarea>
						</div>
						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Project</label>
							<select bind:value={formData.project_id} class="input">
								<option value="">Default Workspace</option>
								{#each projects as project}
									<option value={project.id}>{project.name}</option>
								{/each}
							</select>
						</div>
						<div>
							<label class="block text-sm font-medium text-foreground mb-1.5">Profile</label>
							<select bind:value={formData.profile_id} class="input">
								<option value="">Any Profile</option>
								{#each profiles as profile}
									<option value={profile.id}>{profile.name}</option>
								{/each}
							</select>
						</div>
						<div class="flex items-center gap-3 py-2">
							<label class="relative inline-flex items-center cursor-pointer">
								<input type="checkbox" bind:checked={formData.web_login_allowed} class="sr-only peer" />
								<div class="w-9 h-5 bg-muted rounded-full peer peer-checked:bg-primary transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-4"></div>
							</label>
							<div>
								<span class="text-sm font-medium text-foreground">Allow Web Login</span>
								<p class="text-xs text-muted-foreground">When disabled, user can only access via API key</p>
							</div>
						</div>
						<div class="flex gap-2 pt-2">
							<button onclick={handleSubmit} class="btn btn-primary flex-1">
								{editingUser ? 'Save Changes' : 'Create User'}
							</button>
							<button onclick={resetForm} class="btn btn-secondary flex-1">Cancel</button>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}
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

	.scrollbar-none {
		scrollbar-width: none;
		-ms-overflow-style: none;
	}

	.scrollbar-none::-webkit-scrollbar {
		display: none;
	}
</style>
