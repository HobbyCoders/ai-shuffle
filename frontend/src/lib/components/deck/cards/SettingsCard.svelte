<script lang="ts">
	/**
	 * SettingsCard - Redesigned Modern Settings UI
	 *
	 * Streamlined categories:
	 * - Account: General, Security, Connections (merged)
	 * - AI: API Keys & Model Selection (merged, compact)
	 * - System: Users, Notifications, Cleanup (merged)
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { onMount } from 'svelte';
	import { auth, username, isAdmin, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import { api } from '$lib/api/client';
	import { theme, themePreference, isDark, currentPresetId, themePresets, type Theme } from '$lib/stores/theme';
	import type { ApiUser, ApiUserWithKey, Profile, WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import type { Project } from '$lib/stores/chat';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig, changePassword } from '$lib/api/auth';
	import {
		isSupported as isNotificationsSupported,
		getPermission,
		requestPermission,
		isNotificationsEnabled,
		setNotificationsEnabled
	} from '$lib/services/notifications';
	import WebhookManager from '$lib/components/WebhookManager.svelte';
	import TwoFactorSetup from '$lib/components/TwoFactorSetup.svelte';
	import RateLimitManager from '$lib/components/RateLimitManager.svelte';
	import { slide, fade } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import {
		getUserCredentialPolicies,
		setUserCredentialPolicy,
		type UserCredentialPolicy,
		type CredentialPolicyType
	} from '$lib/api/credentialPolicies';
	import {
		ALL_IMAGE_MODELS,
		ALL_VIDEO_MODELS,
		ALL_TTS_MODELS,
		ALL_STT_MODELS,
		PROVIDER_DISPLAY_NAMES,
		type ImageModel as AIImageModel,
		type VideoModel as AIVideoModel,
		type TTSModel,
		type STTModel,
		type TTSVoice
	} from '$lib/types/ai-models';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// Navigation - simplified to 3 main tabs
	type Tab = 'account' | 'ai' | 'system';
	let activeTab: Tab = $state('account');

	// Collapsible sections state
	let expandedSections: Set<string> = $state(new Set(['general']));

	function toggleSection(section: string) {
		if (expandedSections.has(section)) {
			expandedSections.delete(section);
		} else {
			expandedSections.add(section);
		}
		expandedSections = new Set(expandedSections);
	}

	// Data state
	let apiUsers: ApiUser[] = $state([]);
	let profiles: Profile[] = $state([]);
	let projects: Project[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// Form state for API Users
	let formMode: 'closed' | 'create' | 'edit' = $state('closed');
	let editingUser: ApiUser | null = $state(null);
	let newlyCreatedKey: string | null = $state(null);
	let regeneratedKey: string | null = $state(null);

	// Per-user credential policies
	let userPolicies: Map<string, UserCredentialPolicy[]> = $state(new Map());
	let loadingUserPolicies = $state<string | null>(null);
	let savingUserPolicy = $state<string | null>(null);
	let expandedUserPolicies = $state<string | null>(null);

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
	let claudeOAuthState: string | null = $state(null);
	let claudeAuthCode = $state('');
	let claudeCompletingLogin = $state(false);
	let githubUser: string | null = $state(null);

	// API Keys state
	let openaiApiKey = $state('');
	let openaiApiKeyMasked = $state('');
	let savingOpenaiKey = $state(false);
	let openaiKeySuccess = $state('');
	let openaiKeyError = $state('');

	let meshyApiKey = $state('');
	let meshyApiKeyMasked = $state('');
	let savingMeshyKey = $state(false);
	let meshyKeySuccess = $state('');
	let meshyKeyError = $state('');

	let deepseekApiKey = $state('');
	let deepseekApiKeyMasked = $state('');
	let savingDeepseekKey = $state(false);
	let deepseekKeySuccess = $state('');
	let deepseekKeyError = $state('');

	// Model selection state
	let imageModel = $state('');
	let selectedImageModel = $state('');
	let updatingImageModel = $state(false);
	let imageConfigSuccess = $state('');
	let imageConfigError = $state('');

	let videoModel = $state('');
	let selectedVideoModel = $state('');
	let savingVideoModel = $state(false);
	let videoConfigSuccess = $state('');
	let videoConfigError = $state('');

	let currentTtsModel = $state('');
	let selectedTtsModel = $state('');
	let savingTtsModel = $state(false);
	let selectedTtsVoice = $state('alloy');
	let currentTtsVoice = $state('alloy');
	let ttsSpeed = $state(1.0);
	let ttsOutputFormat = $state('mp3');
	let ttsInstructions = $state('');
	let savingTtsSettings = $state(false);

	let currentSttModel = $state('');
	let selectedSttModel = $state('');
	let savingSttModel = $state(false);
	let audioConfigSuccess = $state('');
	let audioConfigError = $state('');

	let selected3DModel = $state('meshy-6');
	let current3DModel = $state('meshy-6');
	let saving3DModel = $state(false);
	let model3DSuccess = $state('');
	let model3DError = $state('');

	// Password change
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let changingPassword = $state(false);
	let passwordChangeError = $state('');
	let passwordChangeSuccess = $state('');

	// Cleanup settings
	interface CleanupConfig {
		cleanup_interval_minutes: number;
		sdk_session_max_age_minutes: number;
		websocket_max_age_minutes: number;
		sync_log_retention_hours: number;
		cleanup_images_enabled: boolean;
		cleanup_images_max_age_days: number;
		cleanup_videos_enabled: boolean;
		cleanup_videos_max_age_days: number;
		cleanup_shared_files_enabled: boolean;
		cleanup_shared_files_max_age_days: number;
		cleanup_uploads_enabled: boolean;
		cleanup_uploads_max_age_days: number;
		cleanup_project_ids: string[];
		sleep_mode_enabled: boolean;
		sleep_timeout_minutes: number;
	}
	interface CleanupStatus {
		is_sleeping: boolean;
		idle_seconds: number;
		last_activity: string;
		sleep_mode_enabled: boolean;
		sleep_timeout_minutes: number;
		cleanup_interval_minutes: number;
	}
	let cleanupConfig: CleanupConfig | null = $state(null);
	let cleanupStatus: CleanupStatus | null = $state(null);
	let loadingCleanup = $state(true);
	let savingCleanup = $state(false);
	let cleanupSuccess = $state('');
	let cleanupError = $state('');

	// Notifications
	let browserNotificationsEnabled = $state(false);
	let browserNotificationsSupported = $state(false);
	let notificationPermission = $state<NotificationPermission | 'unsupported'>('default');
	let requestingPermission = $state(false);

	// API User form
	let formData = $state({
		name: '',
		description: '',
		project_id: '',
		profile_ids: [] as string[],
		web_login_allowed: true
	});

	// Load data on mount
	$effect(() => {
		if ($isAdmin) {
			loadAllData();
		}
		loadNotificationSettings();
	});

	function loadNotificationSettings() {
		browserNotificationsSupported = isNotificationsSupported();
		notificationPermission = getPermission();
		browserNotificationsEnabled = isNotificationsEnabled();
	}

	async function toggleBrowserNotifications() {
		if (!browserNotificationsSupported) return;
		if (!browserNotificationsEnabled) {
			if (notificationPermission === 'default') {
				requestingPermission = true;
				const result = await requestPermission();
				requestingPermission = false;
				notificationPermission = result;
				if (result !== 'granted') return;
			} else if (notificationPermission === 'denied') {
				return;
			}
		}
		browserNotificationsEnabled = !browserNotificationsEnabled;
		setNotificationsEnabled(browserNotificationsEnabled);
	}

	async function loadAllData() {
		await Promise.all([
			loadData(),
			loadAuthStatus(),
			loadWorkspaceConfig(),
			loadIntegrationSettings(),
			loadCleanupSettings()
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

	async function loadUserPolicies(userId: string) {
		loadingUserPolicies = userId;
		try {
			const response = await getUserCredentialPolicies(userId);
			userPolicies.set(userId, response.policies);
			userPolicies = new Map(userPolicies);
		} catch (e: any) {
			console.error('Failed to load user policies:', e);
		}
		loadingUserPolicies = null;
	}

	async function handleUserPolicyChange(userId: string, credentialType: string, newPolicy: CredentialPolicyType) {
		savingUserPolicy = `${userId}:${credentialType}`;
		try {
			await setUserCredentialPolicy(userId, credentialType, newPolicy);
			await loadUserPolicies(userId);
		} catch (e: any) {
			error = e.detail || 'Failed to update policy';
		}
		savingUserPolicy = null;
	}

	function toggleUserPolicies(userId: string) {
		if (expandedUserPolicies === userId) {
			expandedUserPolicies = null;
		} else {
			expandedUserPolicies = userId;
			if (!userPolicies.has(userId)) {
				loadUserPolicies(userId);
			}
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
				image_model: string | null;
				video_model: string | null;
				tts_model: string | null;
				tts_voice: string | null;
				tts_speed: number | null;
				tts_output_format: string | null;
				tts_instructions: string | null;
				stt_model: string | null;
				meshy_api_key_set: boolean;
				meshy_api_key_masked: string;
				model_3d_model: string | null;
				deepseek_api_key_set: boolean;
				deepseek_api_key_masked: string;
			}>('/settings/integrations');

			openaiApiKeyMasked = settings.openai_api_key_masked;
			imageModel = settings.image_model || '';
			selectedImageModel = settings.image_model || '';
			videoModel = settings.video_model || '';
			selectedVideoModel = settings.video_model || '';
			currentTtsModel = settings.tts_model || 'gpt-4o-mini-tts';
			selectedTtsModel = currentTtsModel;
			currentSttModel = settings.stt_model || 'whisper-1';
			selectedSttModel = currentSttModel;
			selectedTtsVoice = settings.tts_voice || 'alloy';
			currentTtsVoice = selectedTtsVoice;
			ttsSpeed = settings.tts_speed || 1.0;
			ttsOutputFormat = settings.tts_output_format || 'mp3';
			ttsInstructions = settings.tts_instructions || '';
			meshyApiKeyMasked = settings.meshy_api_key_masked || '';
			selected3DModel = settings.model_3d_model || 'meshy-6';
			current3DModel = selected3DModel;
			deepseekApiKeyMasked = settings.deepseek_api_key_masked || '';
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}
	}

	// API Key save functions
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
			openaiKeySuccess = 'Saved';
			setTimeout(() => openaiKeySuccess = '', 3000);
		} catch (e: any) {
			openaiKeyError = e.detail || 'Failed to save';
		} finally {
			savingOpenaiKey = false;
		}
	}

	async function saveMeshyKey() {
		if (!meshyApiKey.trim()) return;
		savingMeshyKey = true;
		meshyKeyError = '';
		meshyKeySuccess = '';
		try {
			await api.post('/meshy/config/api-key', { api_key: meshyApiKey });
			meshyKeySuccess = 'Saved';
			meshyApiKey = '';
			await loadIntegrationSettings();
			setTimeout(() => meshyKeySuccess = '', 3000);
		} catch (e: any) {
			meshyKeyError = e.detail || 'Failed to save';
		}
		savingMeshyKey = false;
	}

	async function saveDeepseekKey() {
		if (!deepseekApiKey.trim()) return;
		savingDeepseekKey = true;
		deepseekKeyError = '';
		deepseekKeySuccess = '';
		try {
			await api.post('/settings/integrations/deepseek', { api_key: deepseekApiKey });
			deepseekKeySuccess = 'Saved';
			deepseekApiKey = '';
			await loadIntegrationSettings();
			setTimeout(() => deepseekKeySuccess = '', 3000);
		} catch (e: any) {
			deepseekKeyError = e.detail || 'Failed to save';
		}
		savingDeepseekKey = false;
	}

	// Model update functions
	async function updateImageModel() {
		if (!selectedImageModel || selectedImageModel === imageModel) return;
		updatingImageModel = true;
		imageConfigError = '';
		imageConfigSuccess = '';
		try {
			await api.patch<{success: boolean, model: string}>('/settings/integrations/image/model', {
				model: selectedImageModel
			});
			imageModel = selectedImageModel;
			imageConfigSuccess = 'Updated';
			setTimeout(() => imageConfigSuccess = '', 3000);
		} catch (e: any) {
			imageConfigError = e.detail || 'Failed to update';
		} finally {
			updatingImageModel = false;
		}
	}

	async function updateVideoModel() {
		if (!selectedVideoModel || selectedVideoModel === videoModel) return;
		savingVideoModel = true;
		videoConfigError = '';
		videoConfigSuccess = '';
		try {
			await api.patch<{success: boolean, model: string}>('/settings/integrations/video/model', {
				model: selectedVideoModel
			});
			videoModel = selectedVideoModel;
			videoConfigSuccess = 'Updated';
			setTimeout(() => videoConfigSuccess = '', 3000);
		} catch (e: any) {
			videoConfigError = e.detail || 'Failed to update';
		} finally {
			savingVideoModel = false;
		}
	}

	async function updateTtsModel() {
		if (!selectedTtsModel || selectedTtsModel === currentTtsModel) return;
		savingTtsModel = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/tts', {
				model: selectedTtsModel
			});
			currentTtsModel = selectedTtsModel;
			const model = ALL_TTS_MODELS.find(m => m.id === selectedTtsModel);
			if (model && model.voices.length > 0) {
				selectedTtsVoice = model.voices[0].id;
				currentTtsVoice = model.voices[0].id;
			}
			audioConfigSuccess = 'Updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: any) {
			audioConfigError = e.detail || 'Failed to update';
		} finally {
			savingTtsModel = false;
		}
	}

	async function updateSttModel() {
		if (!selectedSttModel || selectedSttModel === currentSttModel) return;
		savingSttModel = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/stt', {
				model: selectedSttModel
			});
			currentSttModel = selectedSttModel;
			audioConfigSuccess = 'Updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: any) {
			audioConfigError = e.detail || 'Failed to update';
		} finally {
			savingSttModel = false;
		}
	}

	async function saveTtsSettings() {
		savingTtsSettings = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/tts', {
				model: selectedTtsModel,
				voice: selectedTtsVoice,
				speed: ttsSpeed,
				output_format: ttsOutputFormat,
				instructions: ttsInstructions
			});
			currentTtsModel = selectedTtsModel;
			currentTtsVoice = selectedTtsVoice;
			audioConfigSuccess = 'TTS settings saved';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: any) {
			audioConfigError = e.detail || 'Failed to save';
		} finally {
			savingTtsSettings = false;
		}
	}

	async function save3DModel() {
		if (selected3DModel === current3DModel) return;
		saving3DModel = true;
		model3DError = '';
		model3DSuccess = '';
		try {
			await api.put('/meshy/config/settings', { model: selected3DModel });
			current3DModel = selected3DModel;
			model3DSuccess = 'Updated';
			setTimeout(() => model3DSuccess = '', 3000);
		} catch (e: any) {
			model3DError = e.detail || 'Failed to save';
		}
		saving3DModel = false;
	}

	// Cleanup functions
	async function loadCleanupSettings() {
		loadingCleanup = true;
		try {
			const [config, status] = await Promise.all([
				api.get<CleanupConfig>('/settings/cleanup'),
				api.get<CleanupStatus>('/settings/cleanup/status')
			]);
			cleanupConfig = config;
			cleanupStatus = status;
		} catch (e) {
			console.error('Failed to load cleanup settings:', e);
		} finally {
			loadingCleanup = false;
		}
	}

	async function saveCleanupConfig() {
		if (!cleanupConfig) return;
		savingCleanup = true;
		cleanupError = '';
		cleanupSuccess = '';
		try {
			await api.post('/settings/cleanup', cleanupConfig);
			cleanupSuccess = 'Settings saved';
			setTimeout(() => cleanupSuccess = '', 3000);
		} catch (e: any) {
			cleanupError = e.detail || 'Failed to save';
		} finally {
			savingCleanup = false;
		}
	}

	// Password change
	async function handleChangePassword() {
		if (newPassword !== confirmPassword) {
			passwordChangeError = 'Passwords do not match';
			return;
		}
		if (newPassword.length < 8) {
			passwordChangeError = 'Password must be at least 8 characters';
			return;
		}
		changingPassword = true;
		passwordChangeError = '';
		passwordChangeSuccess = '';
		try {
			await changePassword(currentPassword, newPassword);
			passwordChangeSuccess = 'Password changed';
			currentPassword = '';
			newPassword = '';
			confirmPassword = '';
			setTimeout(() => passwordChangeSuccess = '', 3000);
		} catch (e: any) {
			passwordChangeError = e.detail || 'Failed to change password';
		} finally {
			changingPassword = false;
		}
	}

	// Workspace validation
	async function handleValidateWorkspace() {
		if (!workspacePath.trim()) return;
		validatingWorkspace = true;
		workspaceError = '';
		try {
			workspaceValidation = await validateWorkspacePath(workspacePath);
		} catch (e: any) {
			workspaceError = e.detail || 'Validation failed';
		} finally {
			validatingWorkspace = false;
		}
	}

	async function handleSaveWorkspace() {
		if (!workspacePath.trim()) return;
		savingWorkspace = true;
		workspaceError = '';
		workspaceSuccess = '';
		try {
			await setWorkspaceConfig(workspacePath);
			workspaceSuccess = 'Workspace saved';
			setTimeout(() => workspaceSuccess = '', 3000);
		} catch (e: any) {
			workspaceError = e.detail || 'Failed to save';
		} finally {
			savingWorkspace = false;
		}
	}

	// OAuth functions
	async function startClaudeOAuth() {
		claudeLoginLoading = true;
		try {
			const result = await api.get<{authorization_url: string, state: string}>('/auth/claude/authorize');
			claudeOAuthUrl = result.authorization_url;
			claudeOAuthState = result.state;
			window.open(result.authorization_url, '_blank', 'width=600,height=700');
		} catch (e: any) {
			error = e.detail || 'Failed to start OAuth';
		} finally {
			claudeLoginLoading = false;
		}
	}

	async function completeClaudeOAuth() {
		if (!claudeAuthCode.trim() || !claudeOAuthState) return;
		claudeCompletingLogin = true;
		try {
			await api.post('/auth/claude/callback', {
				code: claudeAuthCode,
				state: claudeOAuthState
			});
			claudeOAuthUrl = null;
			claudeOAuthState = null;
			claudeAuthCode = '';
			await auth.checkAuth();
		} catch (e: any) {
			error = e.detail || 'Failed to complete OAuth';
		} finally {
			claudeCompletingLogin = false;
		}
	}

	async function connectGithub() {
		if (!githubToken.trim()) return;
		githubLoginLoading = true;
		try {
			await api.post('/auth/github/token', { token: githubToken });
			await loadAuthStatus();
			await auth.checkAuth();
			githubToken = '';
		} catch (e: any) {
			error = e.detail || 'Failed to connect GitHub';
		} finally {
			githubLoginLoading = false;
		}
	}

	async function disconnectGithub() {
		try {
			await api.delete('/auth/github/token');
			githubUser = null;
			await auth.checkAuth();
		} catch (e: any) {
			error = e.detail || 'Failed to disconnect';
		}
	}

	// API User functions
	function openCreateForm() {
		formMode = 'create';
		formData = { name: '', description: '', project_id: '', profile_ids: [], web_login_allowed: true };
	}

	function openEditForm(user: ApiUser) {
		formMode = 'edit';
		editingUser = user;
		formData = {
			name: user.name,
			description: user.description || '',
			project_id: user.project_id || '',
			profile_ids: user.profile_ids || [],
			web_login_allowed: user.web_login_allowed ?? true
		};
	}

	function closeForm() {
		formMode = 'closed';
		editingUser = null;
		newlyCreatedKey = null;
	}

	async function saveUser() {
		if (!formData.name.trim()) {
			error = 'Name is required';
			return;
		}
		try {
			if (formMode === 'create') {
				const result = await api.post<ApiUserWithKey>('/api-users', formData);
				newlyCreatedKey = result.api_key;
				await loadData();
			} else if (formMode === 'edit' && editingUser) {
				await api.put(`/api-users/${editingUser.id}`, formData);
				await loadData();
				closeForm();
			}
		} catch (e: any) {
			error = e.detail || 'Failed to save user';
		}
	}

	async function regenerateKey(userId: string) {
		try {
			const result = await api.post<{api_key: string}>(`/api-users/${userId}/regenerate-key`, {});
			regeneratedKey = result.api_key;
		} catch (e: any) {
			error = e.detail || 'Failed to regenerate key';
		}
	}

	async function toggleUserActive(user: ApiUser) {
		try {
			await api.patch(`/api-users/${user.id}/status`, { is_active: !user.is_active });
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to update status';
		}
	}

	async function deleteUser(userId: string) {
		if (!confirm('Delete this user?')) return;
		try {
			await api.delete(`/api-users/${userId}`);
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete user';
		}
	}

	// Helpers
	function getAvailableVoices(): TTSVoice[] {
		const model = ALL_TTS_MODELS.find(m => m.id === currentTtsModel);
		return model?.voices || [];
	}

	function isCurrentModelSteerable(): boolean {
		const model = ALL_TTS_MODELS.find(m => m.id === currentTtsModel);
		return model?.capabilities?.steerability ?? false;
	}

	function formatIdleTime(seconds: number): string {
		if (seconds < 60) return `${seconds}s`;
		if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
		return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
	}

	function groupModelsByProvider<T extends { provider: string }>(models: T[]): Record<string, T[]> {
		const groups: Record<string, T[]> = {};
		for (const model of models) {
			const name = PROVIDER_DISPLAY_NAMES[model.provider] || model.provider;
			if (!groups[name]) groups[name] = [];
			groups[name].push(model);
		}
		return groups;
	}
</script>

<!-- Define settings content as a local snippet to avoid duplication -->
{#snippet settingsContent()}
	<!-- Tab Bar -->
			<nav class="tab-bar">
				<button
					class="tab-btn"
					class:active={activeTab === 'account'}
					onclick={() => activeTab = 'account'}
				>
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
						<circle cx="12" cy="7" r="4"/>
					</svg>
					<span>Account</span>
				</button>
				<button
					class="tab-btn"
					class:active={activeTab === 'ai'}
					onclick={() => activeTab = 'ai'}
				>
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M12 2L2 7l10 5 10-5-10-5z"/>
						<path d="M2 17l10 5 10-5"/>
						<path d="M2 12l10 5 10-5"/>
					</svg>
					<span>AI Models</span>
				</button>
				{#if $isAdmin}
					<button
						class="tab-btn"
						class:active={activeTab === 'system'}
						onclick={() => activeTab = 'system'}
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<circle cx="12" cy="12" r="3"/>
							<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
						</svg>
						<span>System</span>
					</button>
				{/if}
			</nav>

			<!-- Content -->
			<div class="content">
				{#if error}
					<div class="alert error" transition:slide={{ duration: 200 }}>
						<span>{error}</span>
						<button onclick={() => error = ''} class="close-btn">&times;</button>
					</div>
				{/if}

				<!-- ACCOUNT TAB -->
				{#if activeTab === 'account'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<!-- General Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('general')}>
								<h3>General</h3>
								<svg class="chevron" class:expanded={expandedSections.has('general')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('general')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<!-- Theme -->
									<div class="setting-row">
										<div class="setting-info">
											<label>Theme</label>
											<span class="setting-desc">Choose your preferred appearance</span>
										</div>
										<div class="theme-switcher">
											<button
												class="theme-option"
												class:active={$themePreference === 'light'}
												onclick={() => theme.setTheme('light')}
											>
												<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
													<circle cx="12" cy="12" r="5"/>
													<path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
												</svg>
											</button>
											<button
												class="theme-option"
												class:active={$themePreference === 'dark'}
												onclick={() => theme.setTheme('dark')}
											>
												<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
													<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
												</svg>
											</button>
											<button
												class="theme-option"
												class:active={$themePreference === 'system'}
												onclick={() => theme.setTheme('system')}
											>
												<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
													<rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
													<line x1="8" y1="21" x2="16" y2="21"/>
													<line x1="12" y1="17" x2="12" y2="21"/>
												</svg>
											</button>
										</div>
									</div>

									<!-- Theme Preset -->
									<div class="setting-row">
										<div class="setting-info">
											<label for="theme-preset">Theme Style</label>
											<span class="setting-desc">Choose a color palette</span>
										</div>
										<select
											id="theme-preset"
											class="input"
											value={$currentPresetId ?? ''}
											onchange={(e) => theme.setPreset(e.currentTarget.value || null)}
										>
											<option value="">Default</option>
											{#each themePresets as preset}
												<option value={preset.id}>{preset.label}</option>
											{/each}
										</select>
									</div>

									<!-- Workspace Path -->
									<div class="setting-row vertical">
										<div class="setting-info">
											<label>Workspace Path</label>
											<span class="setting-desc">Base directory for projects</span>
										</div>
										<div class="input-group">
											<input
												type="text"
												bind:value={workspacePath}
												placeholder="/path/to/workspace"
												class="input"
											/>
											<button
												onclick={handleValidateWorkspace}
												disabled={validatingWorkspace || !workspacePath.trim()}
												class="btn secondary"
											>
												{validatingWorkspace ? '...' : 'Validate'}
											</button>
											<button
												onclick={handleSaveWorkspace}
												disabled={savingWorkspace || !workspacePath.trim()}
												class="btn primary"
											>
												{savingWorkspace ? '...' : 'Save'}
											</button>
										</div>
										{#if workspaceValidation}
											<div class="validation-result" class:valid={workspaceValidation.valid}>
												{workspaceValidation.valid ? '✓ Valid path' : `✗ ${workspaceValidation.error}`}
											</div>
										{/if}
										{#if workspaceSuccess}
											<div class="success-msg">{workspaceSuccess}</div>
										{/if}
										{#if workspaceError}
											<div class="error-msg">{workspaceError}</div>
										{/if}
									</div>
								</div>
							{/if}
						</section>

						<!-- Security Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('security')}>
								<h3>Security</h3>
								<svg class="chevron" class:expanded={expandedSections.has('security')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('security')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<!-- Password Change -->
									<div class="setting-row vertical">
										<div class="setting-info">
											<label>Change Password</label>
										</div>
										<div class="password-form">
											<input
												type="password"
												bind:value={currentPassword}
												placeholder="Current password"
												class="input"
											/>
											<input
												type="password"
												bind:value={newPassword}
												placeholder="New password (8+ chars)"
												class="input"
											/>
											<input
												type="password"
												bind:value={confirmPassword}
												placeholder="Confirm password"
												class="input"
											/>
											<button
												onclick={handleChangePassword}
												disabled={changingPassword || !currentPassword || !newPassword || !confirmPassword}
												class="btn primary"
											>
												{changingPassword ? 'Changing...' : 'Change Password'}
											</button>
										</div>
										{#if passwordChangeSuccess}
											<div class="success-msg">{passwordChangeSuccess}</div>
										{/if}
										{#if passwordChangeError}
											<div class="error-msg">{passwordChangeError}</div>
										{/if}
									</div>

									<!-- 2FA -->
									<div class="setting-row vertical">
										<div class="setting-info">
											<label>Two-Factor Authentication</label>
											<span class="setting-desc">Add an extra layer of security</span>
										</div>
										<TwoFactorSetup />
									</div>
								</div>
							{/if}
						</section>

						<!-- Connections Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('connections')}>
								<h3>Connections</h3>
								<svg class="chevron" class:expanded={expandedSections.has('connections')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('connections')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<!-- Claude Code -->
									<div class="connection-row">
										<div class="connection-info">
											<div class="connection-icon claude">
												<svg viewBox="0 0 24 24" fill="currentColor">
													<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
												</svg>
											</div>
											<div>
												<span class="connection-name">Claude Code</span>
												{#if $claudeAuthenticated}
													<span class="badge success">Connected</span>
												{/if}
											</div>
										</div>
										<div class="connection-actions">
											{#if $claudeAuthenticated}
												<span class="connected-text">Authenticated</span>
											{:else if claudeOAuthUrl}
												<div class="oauth-flow">
													<input
														type="text"
														bind:value={claudeAuthCode}
														placeholder="Paste code from browser"
														class="input compact"
													/>
													<button
														onclick={completeClaudeOAuth}
														disabled={claudeCompletingLogin || !claudeAuthCode}
														class="btn primary compact"
													>
														{claudeCompletingLogin ? '...' : 'Complete'}
													</button>
												</div>
											{:else}
												<button
													onclick={startClaudeOAuth}
													disabled={claudeLoginLoading}
													class="btn primary compact"
												>
													{claudeLoginLoading ? '...' : 'Connect'}
												</button>
											{/if}
										</div>
									</div>

									<!-- GitHub -->
									<div class="connection-row">
										<div class="connection-info">
											<div class="connection-icon github">
												<svg viewBox="0 0 24 24" fill="currentColor">
													<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
												</svg>
											</div>
											<div>
												<span class="connection-name">GitHub</span>
												{#if githubUser}
													<span class="badge success">@{githubUser}</span>
												{/if}
											</div>
										</div>
										<div class="connection-actions">
											{#if githubUser}
												<button onclick={disconnectGithub} class="btn danger compact">Disconnect</button>
											{:else}
												<div class="oauth-flow">
													<input
														type="password"
														bind:value={githubToken}
														placeholder="Personal access token"
														class="input compact"
													/>
													<button
														onclick={connectGithub}
														disabled={githubLoginLoading || !githubToken}
														class="btn primary compact"
													>
														{githubLoginLoading ? '...' : 'Connect'}
													</button>
												</div>
											{/if}
										</div>
									</div>
								</div>
							{/if}
						</section>
					</div>
				{/if}

				<!-- AI TAB -->
				{#if activeTab === 'ai'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<!-- API Keys Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('api-keys')}>
								<h3>API Keys</h3>
								<svg class="chevron" class:expanded={expandedSections.has('api-keys')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('api-keys')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<!-- OpenAI -->
									<div class="api-key-row">
										<div class="api-key-info">
											<span class="api-key-name">OpenAI</span>
											{#if openaiApiKeyMasked}
												<code class="masked-key">{openaiApiKeyMasked}</code>
											{/if}
										</div>
										<div class="api-key-input">
											<input
												type="password"
												bind:value={openaiApiKey}
												placeholder={openaiApiKeyMasked ? 'Enter new key...' : 'sk-...'}
												class="input compact"
											/>
											<button
												onclick={saveOpenaiApiKey}
												disabled={savingOpenaiKey || !openaiApiKey.trim()}
												class="btn primary compact"
											>
												{savingOpenaiKey ? '...' : 'Save'}
											</button>
										</div>
										{#if openaiKeySuccess}<span class="inline-success">{openaiKeySuccess}</span>{/if}
										{#if openaiKeyError}<span class="inline-error">{openaiKeyError}</span>{/if}
									</div>

									<!-- Meshy -->
									<div class="api-key-row">
										<div class="api-key-info">
											<span class="api-key-name">Meshy (3D)</span>
											{#if meshyApiKeyMasked}
												<code class="masked-key">{meshyApiKeyMasked}</code>
											{/if}
										</div>
										<div class="api-key-input">
											<input
												type="password"
												bind:value={meshyApiKey}
												placeholder={meshyApiKeyMasked ? 'Enter new key...' : 'API key'}
												class="input compact"
											/>
											<button
												onclick={saveMeshyKey}
												disabled={savingMeshyKey || !meshyApiKey.trim()}
												class="btn primary compact"
											>
												{savingMeshyKey ? '...' : 'Save'}
											</button>
										</div>
										{#if meshyKeySuccess}<span class="inline-success">{meshyKeySuccess}</span>{/if}
										{#if meshyKeyError}<span class="inline-error">{meshyKeyError}</span>{/if}
									</div>

									<!-- DeepSeek -->
									<div class="api-key-row">
										<div class="api-key-info">
											<span class="api-key-name">DeepSeek</span>
											{#if deepseekApiKeyMasked}
												<code class="masked-key">{deepseekApiKeyMasked}</code>
											{/if}
										</div>
										<div class="api-key-input">
											<input
												type="password"
												bind:value={deepseekApiKey}
												placeholder={deepseekApiKeyMasked ? 'Enter new key...' : 'sk-...'}
												class="input compact"
											/>
											<button
												onclick={saveDeepseekKey}
												disabled={savingDeepseekKey || !deepseekApiKey.trim()}
												class="btn primary compact"
											>
												{savingDeepseekKey ? '...' : 'Save'}
											</button>
										</div>
										{#if deepseekKeySuccess}<span class="inline-success">{deepseekKeySuccess}</span>{/if}
										{#if deepseekKeyError}<span class="inline-error">{deepseekKeyError}</span>{/if}
									</div>
								</div>
							{/if}
						</section>

						<!-- Image Models Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('image-models')}>
								<h3>Image Generation</h3>
								<svg class="chevron" class:expanded={expandedSections.has('image-models')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('image-models')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<div class="model-selector">
										<select
											bind:value={selectedImageModel}
											onchange={updateImageModel}
											disabled={updatingImageModel}
											class="input select"
										>
											{#each Object.entries(groupModelsByProvider(ALL_IMAGE_MODELS)) as [provider, models]}
												<optgroup label={provider}>
													{#each models as model}
														<option value={model.id}>
															{model.displayName} — ${model.pricePerImage.toFixed(2)}/img
														</option>
													{/each}
												</optgroup>
											{/each}
										</select>
										{#if updatingImageModel}
											<span class="loading-indicator">Updating...</span>
										{/if}
									</div>
									{#if imageConfigSuccess}<div class="success-msg">{imageConfigSuccess}</div>{/if}
									{#if imageConfigError}<div class="error-msg">{imageConfigError}</div>{/if}
								</div>
							{/if}
						</section>

						<!-- Video Models Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('video-models')}>
								<h3>Video Generation</h3>
								<svg class="chevron" class:expanded={expandedSections.has('video-models')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('video-models')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<div class="model-selector">
										<select
											bind:value={selectedVideoModel}
											onchange={updateVideoModel}
											disabled={savingVideoModel}
											class="input select"
										>
											{#each Object.entries(groupModelsByProvider(ALL_VIDEO_MODELS)) as [provider, models]}
												<optgroup label={provider}>
													{#each models as model}
														<option value={model.id}>
															{model.displayName} — ${model.pricePerSecond.toFixed(2)}/sec, max {model.maxDuration}s
														</option>
													{/each}
												</optgroup>
											{/each}
										</select>
										{#if savingVideoModel}
											<span class="loading-indicator">Updating...</span>
										{/if}
									</div>
									{#if videoConfigSuccess}<div class="success-msg">{videoConfigSuccess}</div>{/if}
									{#if videoConfigError}<div class="error-msg">{videoConfigError}</div>{/if}
								</div>
							{/if}
						</section>

						<!-- Audio Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('audio-models')}>
								<h3>Audio (TTS/STT)</h3>
								<svg class="chevron" class:expanded={expandedSections.has('audio-models')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('audio-models')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<!-- TTS Model -->
									<div class="setting-row">
										<label class="setting-label">Text-to-Speech Model</label>
										<select
											bind:value={selectedTtsModel}
											onchange={updateTtsModel}
											disabled={savingTtsModel}
											class="input select"
										>
											{#each Object.entries(groupModelsByProvider(ALL_TTS_MODELS)) as [provider, models]}
												<optgroup label={provider}>
													{#each models as model}
														<option value={model.id}>{model.displayName}</option>
													{/each}
												</optgroup>
											{/each}
										</select>
									</div>

									<!-- TTS Voice -->
									<div class="setting-row">
										<label class="setting-label">Voice</label>
										<select bind:value={selectedTtsVoice} class="input select">
											{#each getAvailableVoices() as voice}
												<option value={voice.id}>{voice.name} {voice.gender ? `(${voice.gender})` : ''}</option>
											{/each}
											{#if getAvailableVoices().length === 0}
												<option value="alloy">Alloy</option>
												<option value="echo">Echo</option>
												<option value="fable">Fable</option>
												<option value="onyx">Onyx</option>
												<option value="nova">Nova</option>
												<option value="shimmer">Shimmer</option>
											{/if}
										</select>
									</div>

									<!-- TTS Speed -->
									<div class="setting-row">
										<label class="setting-label">Speed ({ttsSpeed.toFixed(1)}x)</label>
										<input type="range" min="0.25" max="4.0" step="0.1" bind:value={ttsSpeed} class="slider" />
									</div>

									<!-- TTS Format -->
									<div class="setting-row">
										<label class="setting-label">Output Format</label>
										<select bind:value={ttsOutputFormat} class="input select compact">
											<option value="mp3">MP3</option>
											<option value="wav">WAV</option>
											<option value="opus">Opus</option>
											<option value="aac">AAC</option>
											<option value="flac">FLAC</option>
										</select>
									</div>

									<!-- Voice Instructions (if steerable) -->
									{#if isCurrentModelSteerable()}
										<div class="setting-row vertical">
											<label class="setting-label">Voice Instructions <span class="badge primary">Steerable</span></label>
											<textarea
												bind:value={ttsInstructions}
												placeholder="e.g., Speak in a warm, friendly tone..."
												class="input textarea"
												rows="2"
											></textarea>
										</div>
									{/if}

									<button
										onclick={saveTtsSettings}
										disabled={savingTtsSettings}
										class="btn primary"
									>
										{savingTtsSettings ? 'Saving...' : 'Save TTS Settings'}
									</button>

									<hr class="divider" />

									<!-- STT Model -->
									<div class="setting-row">
										<label class="setting-label">Speech-to-Text Model</label>
										<select
											bind:value={selectedSttModel}
											onchange={updateSttModel}
											disabled={savingSttModel}
											class="input select"
										>
											{#each Object.entries(groupModelsByProvider(ALL_STT_MODELS)) as [provider, models]}
												<optgroup label={provider}>
													{#each models as model}
														<option value={model.id}>{model.displayName}</option>
													{/each}
												</optgroup>
											{/each}
										</select>
									</div>

									{#if audioConfigSuccess}<div class="success-msg">{audioConfigSuccess}</div>{/if}
									{#if audioConfigError}<div class="error-msg">{audioConfigError}</div>{/if}
								</div>
							{/if}
						</section>

						<!-- 3D Models Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('3d-models')}>
								<h3>3D Generation</h3>
								<svg class="chevron" class:expanded={expandedSections.has('3d-models')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('3d-models')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<div class="model-selector">
										<select
											bind:value={selected3DModel}
											onchange={save3DModel}
											disabled={saving3DModel}
											class="input select"
										>
											<option value="meshy-6">Meshy 6 (Latest) — Best Quality</option>
											<option value="meshy-5">Meshy 5 — Balanced</option>
											<option value="meshy-4">Meshy 4 — Fast</option>
										</select>
										{#if saving3DModel}
											<span class="loading-indicator">Updating...</span>
										{/if}
									</div>
									{#if model3DSuccess}<div class="success-msg">{model3DSuccess}</div>{/if}
									{#if model3DError}<div class="error-msg">{model3DError}</div>{/if}
								</div>
							{/if}
						</section>
					</div>
				{/if}

				<!-- SYSTEM TAB -->
				{#if activeTab === 'system' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<!-- API Users Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('users')}>
								<h3>API Users</h3>
								<svg class="chevron" class:expanded={expandedSections.has('users')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('users')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									{#if loading}
										<div class="loading-text">Loading...</div>
									{:else}
										<button onclick={openCreateForm} class="btn primary compact mb-3">
											+ New User
										</button>

										{#if formMode !== 'closed'}
											<div class="user-form" transition:slide={{ duration: 200 }}>
												<h4>{formMode === 'create' ? 'Create User' : 'Edit User'}</h4>
												<input
													type="text"
													bind:value={formData.name}
													placeholder="Username"
													class="input"
												/>
												<input
													type="text"
													bind:value={formData.description}
													placeholder="Description (optional)"
													class="input"
												/>
												<select bind:value={formData.project_id} class="input select">
													<option value="">No project restriction</option>
													{#each projects as project}
														<option value={project.id}>{project.name}</option>
													{/each}
												</select>
												<label class="checkbox-label">
													<input type="checkbox" bind:checked={formData.web_login_allowed} />
													<span>Allow web login</span>
												</label>
												<div class="form-actions">
													<button onclick={closeForm} class="btn secondary compact">Cancel</button>
													<button onclick={saveUser} class="btn primary compact">
														{formMode === 'create' ? 'Create' : 'Save'}
													</button>
												</div>
												{#if newlyCreatedKey}
													<div class="api-key-display">
														<strong>API Key (save now!):</strong>
														<code>{newlyCreatedKey}</code>
													</div>
												{/if}
											</div>
										{/if}

										<div class="users-list">
											{#each apiUsers as user}
												<div class="user-row" class:inactive={!user.is_active}>
													<div class="user-info">
														<span class="user-name">{user.name}</span>
														{#if !user.is_active}
															<span class="badge muted">Inactive</span>
														{/if}
														{#if user.description}
															<span class="user-desc">{user.description}</span>
														{/if}
													</div>
													<div class="user-actions">
														<button onclick={() => openEditForm(user)} class="icon-btn" title="Edit">
															<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
																<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
																<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
															</svg>
														</button>
														<button onclick={() => regenerateKey(user.id)} class="icon-btn" title="Regenerate Key">
															<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
																<polyline points="23 4 23 10 17 10"/>
																<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
															</svg>
														</button>
														<button onclick={() => toggleUserActive(user)} class="icon-btn" title={user.is_active ? 'Deactivate' : 'Activate'}>
															{#if user.is_active}
																<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
																	<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>
																	<line x1="12" y1="2" x2="12" y2="12"/>
																</svg>
															{:else}
																<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
																	<path d="M5 12.55a11 11 0 0 1 14.08 0"/>
																	<path d="M1.42 9a16 16 0 0 1 21.16 0"/>
																	<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>
																	<circle cx="12" cy="20" r="1"/>
																</svg>
															{/if}
														</button>
														<button onclick={() => deleteUser(user.id)} class="icon-btn danger" title="Delete">
															<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
																<polyline points="3 6 5 6 21 6"/>
																<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
															</svg>
														</button>
													</div>
												</div>
												{#if regeneratedKey && editingUser?.id === user.id}
													<div class="api-key-display" transition:slide>
														<strong>New API Key:</strong>
														<code>{regeneratedKey}</code>
													</div>
												{/if}
											{/each}
										</div>
									{/if}
								</div>
							{/if}
						</section>

						<!-- Rate Limits Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('rate-limits')}>
								<h3>Rate Limits</h3>
								<svg class="chevron" class:expanded={expandedSections.has('rate-limits')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('rate-limits')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<RateLimitManager />
								</div>
							{/if}
						</section>

						<!-- Notifications Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('notifications')}>
								<h3>Notifications</h3>
								<svg class="chevron" class:expanded={expandedSections.has('notifications')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('notifications')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<!-- Browser Notifications -->
									<div class="setting-row">
										<div class="setting-info">
											<label>Browser Notifications</label>
											<span class="setting-desc">
												{#if !browserNotificationsSupported}
													Not supported in this browser
												{:else if notificationPermission === 'denied'}
													Blocked — enable in browser settings
												{:else}
													Get notified when tasks complete
												{/if}
											</span>
										</div>
										<button
											onclick={toggleBrowserNotifications}
											disabled={!browserNotificationsSupported || notificationPermission === 'denied' || requestingPermission}
											class="toggle"
											class:active={browserNotificationsEnabled}
										>
											<span class="toggle-knob"></span>
										</button>
									</div>

									<hr class="divider" />

									<!-- Webhooks -->
									<h4 class="subsection-title">Webhooks</h4>
									<WebhookManager />
								</div>
							{/if}
						</section>

						<!-- Cleanup Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('cleanup')}>
								<h3>Background Cleanup</h3>
								<svg class="chevron" class:expanded={expandedSections.has('cleanup')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('cleanup')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									{#if loadingCleanup}
										<div class="loading-text">Loading...</div>
									{:else if cleanupConfig}
										<!-- Status -->
										<div class="cleanup-status" class:sleeping={cleanupStatus?.is_sleeping}>
											<span class="status-icon">{cleanupStatus?.is_sleeping ? '😴' : '⚡'}</span>
											<div class="status-info">
												<strong>{cleanupStatus?.is_sleeping ? 'Sleeping' : 'Active'}</strong>
												<span>Idle {formatIdleTime(cleanupStatus?.idle_seconds || 0)}</span>
											</div>
										</div>

										<!-- Sleep Mode -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Sleep Mode</label>
												<span class="setting-desc">Pause cleanup when inactive</span>
											</div>
											<button
												onclick={() => cleanupConfig && (cleanupConfig.sleep_mode_enabled = !cleanupConfig.sleep_mode_enabled)}
												class="toggle"
												class:active={cleanupConfig.sleep_mode_enabled}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										{#if cleanupConfig.sleep_mode_enabled}
											<div class="setting-row">
												<label class="setting-label">Sleep after (min)</label>
												<input type="number" min="1" max="60" bind:value={cleanupConfig.sleep_timeout_minutes} class="input compact number" />
											</div>
										{/if}

										<hr class="divider" />

										<!-- File Cleanup Toggles -->
										<h4 class="subsection-title">File Cleanup</h4>

										<div class="cleanup-grid">
											<div class="cleanup-item">
												<span>Images</span>
												<div class="cleanup-controls">
													{#if cleanupConfig.cleanup_images_enabled}
														<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_images_max_age_days} class="input compact number" />
														<span class="unit">days</span>
													{/if}
													<button
														onclick={() => cleanupConfig && (cleanupConfig.cleanup_images_enabled = !cleanupConfig.cleanup_images_enabled)}
														class="toggle small"
														class:active={cleanupConfig.cleanup_images_enabled}
													>
														<span class="toggle-knob"></span>
													</button>
												</div>
											</div>

											<div class="cleanup-item">
												<span>Videos</span>
												<div class="cleanup-controls">
													{#if cleanupConfig.cleanup_videos_enabled}
														<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_videos_max_age_days} class="input compact number" />
														<span class="unit">days</span>
													{/if}
													<button
														onclick={() => cleanupConfig && (cleanupConfig.cleanup_videos_enabled = !cleanupConfig.cleanup_videos_enabled)}
														class="toggle small"
														class:active={cleanupConfig.cleanup_videos_enabled}
													>
														<span class="toggle-knob"></span>
													</button>
												</div>
											</div>

											<div class="cleanup-item">
												<span>Shared Files</span>
												<div class="cleanup-controls">
													{#if cleanupConfig.cleanup_shared_files_enabled}
														<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_shared_files_max_age_days} class="input compact number" />
														<span class="unit">days</span>
													{/if}
													<button
														onclick={() => cleanupConfig && (cleanupConfig.cleanup_shared_files_enabled = !cleanupConfig.cleanup_shared_files_enabled)}
														class="toggle small"
														class:active={cleanupConfig.cleanup_shared_files_enabled}
													>
														<span class="toggle-knob"></span>
													</button>
												</div>
											</div>

											<div class="cleanup-item">
												<span>Uploads</span>
												<div class="cleanup-controls">
													{#if cleanupConfig.cleanup_uploads_enabled}
														<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_uploads_max_age_days} class="input compact number" />
														<span class="unit">days</span>
													{/if}
													<button
														onclick={() => cleanupConfig && (cleanupConfig.cleanup_uploads_enabled = !cleanupConfig.cleanup_uploads_enabled)}
														class="toggle small"
														class:active={cleanupConfig.cleanup_uploads_enabled}
													>
														<span class="toggle-knob"></span>
													</button>
												</div>
											</div>
										</div>

										<hr class="divider" />

										<!-- Session Timeouts -->
										<h4 class="subsection-title">Session Timeouts</h4>
										<div class="timeout-grid">
											<div class="timeout-item">
												<label>SDK Session</label>
												<div class="timeout-input">
													<input type="number" min="5" max="480" bind:value={cleanupConfig.sdk_session_max_age_minutes} class="input compact number" />
													<span class="unit">min</span>
												</div>
											</div>
											<div class="timeout-item">
												<label>WebSocket</label>
												<div class="timeout-input">
													<input type="number" min="1" max="60" bind:value={cleanupConfig.websocket_max_age_minutes} class="input compact number" />
													<span class="unit">min</span>
												</div>
											</div>
											<div class="timeout-item">
												<label>Sync Logs</label>
												<div class="timeout-input">
													<input type="number" min="1" max="168" bind:value={cleanupConfig.sync_log_retention_hours} class="input compact number" />
													<span class="unit">hrs</span>
												</div>
											</div>
										</div>

										{#if cleanupSuccess}<div class="success-msg">{cleanupSuccess}</div>{/if}
										{#if cleanupError}<div class="error-msg">{cleanupError}</div>{/if}

										<button
											onclick={saveCleanupConfig}
											disabled={savingCleanup}
											class="btn primary mt-3"
										>
											{savingCleanup ? 'Saving...' : 'Save Cleanup Settings'}
										</button>
									{/if}
								</div>
							{/if}
						</section>
					</div>
				{/if}
			</div>
{/snippet}

<!-- Conditional wrapper: Mobile uses no BaseCard, Desktop uses BaseCard -->
{#if mobile}
	<div class="settings mobile">
		{@render settingsContent()}
	</div>
{:else}
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		{#snippet children()}
			<div class="settings">
				{@render settingsContent()}
			</div>
		{/snippet}
	</BaseCard>
{/if}

<style>
	/* ========================================
	   SETTINGS - Modern Redesign
	   Theme-compliant using CSS variables
	   ======================================== */

	.settings {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--card);
		color: var(--card-foreground);
		font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif;
	}

	/* Tab Bar */
	.tab-bar {
		display: flex;
		gap: 2px;
		padding: 12px 16px;
		background: var(--muted);
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.tab-btn {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 16px;
		background: transparent;
		border: none;
		border-radius: var(--radius);
		color: var(--muted-foreground);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.tab-btn:hover {
		background: var(--accent);
		color: var(--accent-foreground);
	}

	.tab-btn.active {
		background: var(--primary);
		color: var(--primary-foreground);
		box-shadow: var(--shadow-s);
	}

	.tab-btn svg {
		width: 18px;
		height: 18px;
		/* Ensure icon inherits color from parent button */
		stroke: currentColor;
		transition: stroke 150ms ease;
	}

	/* Content Area */
	.content {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
	}

	.tab-content {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* Sections */
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover {
		background: var(--accent);
	}

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded {
		transform: rotate(180deg);
	}

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	/* Setting Rows */
	.setting-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 8px 0;
	}

	.setting-row.vertical {
		flex-direction: column;
		align-items: stretch;
	}

	.setting-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.setting-info label,
	.setting-label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.setting-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Inputs */
	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.input::placeholder {
		color: var(--muted-foreground);
	}

	.input.compact {
		padding: 6px 10px;
		font-size: 0.8125rem;
	}

	.input.number {
		width: 70px;
		text-align: center;
	}

	/* Note: Select styling is handled by global select.input in app.css */
	/* Do NOT add appearance/background-image here to avoid duplicate arrows */

	.input.textarea {
		resize: vertical;
		min-height: 60px;
	}

	.input-group {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.input-group .input {
		flex: 1;
	}

	/* Buttons */
	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 8px 16px;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn.primary:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn.secondary {
		background: var(--secondary);
		color: var(--secondary-foreground);
		border: 1px solid var(--border);
	}

	.btn.secondary:hover:not(:disabled) {
		background: var(--accent);
	}

	.btn.danger {
		background: var(--destructive);
		color: var(--destructive-foreground);
	}

	.btn.compact {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	/* Toggle Switch */
	.toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 12px;
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
	}

	.toggle:hover {
		border-color: var(--ring);
	}

	.toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.toggle.small {
		width: 36px;
		height: 20px;
		border-radius: 10px;
	}

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: white;
		border-radius: 50%;
		transition: transform 200ms ease;
		box-shadow: var(--shadow-s);
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}

	.toggle.small .toggle-knob {
		width: 14px;
		height: 14px;
	}

	.toggle.small.active .toggle-knob {
		transform: translateX(16px);
	}

	/* Theme Switcher */
	.theme-switcher {
		display: flex;
		gap: 4px;
		background: var(--muted);
		padding: 4px;
		border-radius: var(--radius);
	}

	.theme-option {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 32px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.theme-option:hover {
		background: var(--accent);
		color: var(--accent-foreground);
	}

	.theme-option.active {
		background: var(--card);
		color: var(--primary);
		box-shadow: var(--shadow-s);
	}

	.theme-option svg {
		width: 18px;
		height: 18px;
	}

	/* Badges */
	.badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.025em;
	}

	.badge.success {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
	}

	.badge.muted {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.badge.primary {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	/* Alerts & Messages */
	.alert {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 14px;
		border-radius: var(--radius);
		font-size: 0.875rem;
		margin-bottom: 12px;
	}

	.alert.error {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	.close-btn {
		background: none;
		border: none;
		font-size: 1.25rem;
		cursor: pointer;
		opacity: 0.7;
		color: inherit;
		line-height: 1;
	}

	.close-btn:hover {
		opacity: 1;
	}

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}

	.inline-success {
		font-size: 0.75rem;
		color: var(--success);
		margin-left: 8px;
	}

	.inline-error {
		font-size: 0.75rem;
		color: var(--destructive);
		margin-left: 8px;
	}

	.validation-result {
		font-size: 0.8125rem;
		padding: 6px 10px;
		border-radius: calc(var(--radius) - 2px);
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		color: var(--destructive);
	}

	.validation-result.valid {
		background: color-mix(in oklch, var(--success) 10%, transparent);
		color: var(--success);
	}

	/* Password Form */
	.password-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* Connections */
	.connection-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 12px;
		background: var(--muted);
		border-radius: var(--radius);
	}

	.connection-info {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.connection-icon {
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: 8px;
		color: white;
	}

	.connection-icon.claude {
		background: linear-gradient(135deg, #d97706, #b45309);
	}

	.connection-icon.github {
		background: #24292e;
	}

	.connection-icon svg {
		width: 20px;
		height: 20px;
	}

	.connection-name {
		font-weight: 500;
		font-size: 0.875rem;
	}

	.connection-actions {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.oauth-flow {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.connected-text {
		font-size: 0.8125rem;
		color: var(--success);
	}

	/* API Keys */
	.api-key-row {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 12px;
		background: var(--muted);
		border-radius: var(--radius);
	}

	.api-key-info {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.api-key-name {
		font-weight: 500;
		font-size: 0.875rem;
	}

	.masked-key {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: var(--secondary);
		padding: 2px 6px;
		border-radius: 4px;
	}

	.api-key-input {
		display: flex;
		gap: 6px;
	}

	.api-key-input .input {
		flex: 1;
	}

	/* Model Selectors */
	.model-selector {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.model-selector .input {
		flex: 1;
	}

	.loading-indicator {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Slider */
	.slider {
		width: 100%;
		height: 6px;
		border-radius: 3px;
		background: var(--muted);
		appearance: none;
		cursor: pointer;
	}

	.slider::-webkit-slider-thumb {
		appearance: none;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
		box-shadow: var(--shadow-s);
	}

	.slider::-moz-range-thumb {
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
		border: none;
	}

	/* Dividers */
	.divider {
		border: none;
		border-top: 1px solid var(--border);
		margin: 12px 0;
	}

	.subsection-title {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--muted-foreground);
		margin: 8px 0 4px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	/* Users */
	.users-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.user-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 12px;
		background: var(--muted);
		border-radius: var(--radius);
		transition: opacity 150ms ease;
	}

	.user-row.inactive {
		opacity: 0.6;
	}

	.user-info {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}

	.user-name {
		font-weight: 500;
		font-size: 0.875rem;
	}

	.user-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.user-actions {
		display: flex;
		gap: 4px;
	}

	.icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.icon-btn:hover {
		background: var(--accent);
		color: var(--accent-foreground);
	}

	.icon-btn.danger:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	.icon-btn svg {
		width: 16px;
		height: 16px;
	}

	.user-form {
		padding: 16px;
		background: var(--muted);
		border-radius: var(--radius);
		display: flex;
		flex-direction: column;
		gap: 10px;
		margin-bottom: 12px;
	}

	.user-form h4 {
		margin: 0 0 4px;
		font-size: 0.9375rem;
		font-weight: 600;
	}

	.form-actions {
		display: flex;
		gap: 8px;
		justify-content: flex-end;
		margin-top: 4px;
	}

	.checkbox-label {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.875rem;
		cursor: pointer;
	}

	.checkbox-label input {
		width: 16px;
		height: 16px;
		accent-color: var(--primary);
	}

	.api-key-display {
		padding: 12px;
		background: color-mix(in oklch, var(--warning) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
		border-radius: var(--radius);
		font-size: 0.8125rem;
	}

	.api-key-display code {
		display: block;
		margin-top: 6px;
		padding: 8px;
		background: var(--muted);
		border-radius: 4px;
		font-family: monospace;
		font-size: 0.75rem;
		word-break: break-all;
	}

	/* Cleanup */
	.cleanup-status {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px;
		background: color-mix(in oklch, var(--success) 10%, transparent);
		border-radius: var(--radius);
		margin-bottom: 8px;
	}

	.cleanup-status.sleeping {
		background: var(--muted);
	}

	.status-icon {
		font-size: 1.5rem;
	}

	.status-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.status-info strong {
		font-size: 0.875rem;
	}

	.status-info span {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.cleanup-grid {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.cleanup-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 8px 0;
	}

	.cleanup-item span {
		font-size: 0.875rem;
	}

	.cleanup-controls {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.unit {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		min-width: 28px;
	}

	.timeout-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 12px;
	}

	.timeout-item {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.timeout-item label {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	.timeout-input {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	/* Utility */
	.loading-text {
		text-align: center;
		padding: 20px;
		color: var(--muted-foreground);
	}

	.mb-3 {
		margin-bottom: 12px;
	}

	.mt-3 {
		margin-top: 12px;
	}

	/* Mobile adjustments */
	.settings.mobile .tab-bar {
		padding: 8px 12px;
	}

	.settings.mobile .tab-btn {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	.settings.mobile .tab-btn span {
		display: none;
	}

	.settings.mobile .content {
		padding: 12px;
	}

	.settings.mobile .connection-row,
	.settings.mobile .api-key-row {
		flex-direction: column;
		align-items: stretch;
	}

	.settings.mobile .connection-actions,
	.settings.mobile .api-key-input {
		flex-direction: column;
	}

	.settings.mobile .oauth-flow {
		flex-direction: column;
	}
</style>
