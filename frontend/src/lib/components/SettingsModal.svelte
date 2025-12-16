<script lang="ts">
	/**
	 * SettingsModal - Redesigned with sidebar navigation and grouped categories
	 *
	 * Categories:
	 * - Account: General settings, Security, Authentication
	 * - API & Users: API Users management, Rate Limits
	 * - AI Models: API Keys, Image/Video/Audio model selection
	 * - System: Notifications, Background Cleanup
	 */
	import { onMount } from 'svelte';
	import { auth, username, isAdmin, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import { api } from '$lib/api/client';
	import { theme, themePreference, isDark, type Theme } from '$lib/stores/theme';
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
	import WebhookManager from './WebhookManager.svelte';
	import TwoFactorSetup from './TwoFactorSetup.svelte';
	import RateLimitManager from './RateLimitManager.svelte';

	interface Props {
		open: boolean;
		onClose: () => void;
	}

	let { open, onClose }: Props = $props();

	// Navigation state
	type Category = 'account' | 'api-users' | 'ai-models' | 'system';
	type Section =
		| 'general' | 'security' | 'authentication'
		| 'users' | 'rate-limits'
		| 'api-keys' | 'models'
		| 'notifications' | 'cleanup';

	let activeCategory: Category = $state('account');
	let activeSection: Section = $state('general');
	let mobileMenuOpen = $state(false);

	// Category definitions
	const categories: { id: Category; label: string; icon: string; sections: { id: Section; label: string }[] }[] = [
		{
			id: 'account',
			label: 'Account',
			icon: 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z',
			sections: [
				{ id: 'general', label: 'General' },
				{ id: 'security', label: 'Security' },
				{ id: 'authentication', label: 'Connections' }
			]
		},
		{
			id: 'api-users',
			label: 'API & Users',
			icon: 'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z',
			sections: [
				{ id: 'users', label: 'API Users' },
				{ id: 'rate-limits', label: 'Rate Limits' }
			]
		},
		{
			id: 'ai-models',
			label: 'AI Models',
			icon: 'M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z',
			sections: [
				{ id: 'api-keys', label: 'API Keys' },
				{ id: 'models', label: 'Models' }
			]
		},
		{
			id: 'system',
			label: 'System',
			icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z',
			sections: [
				{ id: 'notifications', label: 'Notifications' },
				{ id: 'cleanup', label: 'Cleanup' }
			]
		}
	];

	function selectCategory(cat: Category) {
		activeCategory = cat;
		const category = categories.find(c => c.id === cat);
		if (category && category.sections.length > 0) {
			activeSection = category.sections[0].id;
		}
		mobileMenuOpen = false;
	}

	function selectSection(section: Section) {
		activeSection = section;
	}

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
	let claudeOAuthState: string | null = $state(null);
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

	// Cleanup settings state
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
	interface CleanupPreview {
		images: { name: string; size: number; project: string; age_days: number }[];
		videos: { name: string; size: number; project: string; age_days: number }[];
		shared_files: { name: string; size: number; project: string; age_days: number }[];
		total_count: number;
		total_bytes: number;
		total_bytes_formatted: string;
	}
	let cleanupConfig: CleanupConfig | null = $state(null);
	let cleanupStatus: CleanupStatus | null = $state(null);
	let cleanupPreview: CleanupPreview | null = $state(null);
	let loadingCleanup = $state(true);
	let savingCleanup = $state(false);
	let runningCleanup = $state(false);
	let cleanupSuccess = $state('');
	let cleanupError = $state('');
	let showCleanupPreview = $state(false);

	// Browser notification state
	let browserNotificationsEnabled = $state(false);
	let browserNotificationsSupported = $state(false);
	let notificationPermission = $state<NotificationPermission | 'unsupported'>('default');
	let requestingPermission = $state(false);

	// API User form data
	let formData = $state({
		name: '',
		description: '',
		project_id: '',
		profile_id: '',
		web_login_allowed: true
	});

	// Load data when modal opens
	$effect(() => {
		if (open && $isAdmin) {
			loadAllData();
		}
	});

	// Initialize notification settings on mount
	$effect(() => {
		if (open) {
			loadNotificationSettings();
		}
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

				if (result !== 'granted') {
					return;
				}
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
			setTimeout(() => openaiKeySuccess = '', 3000);
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
			setTimeout(() => imageConfigSuccess = '', 3000);
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
			imageConfigSuccess = 'Image model updated';
			setTimeout(() => imageConfigSuccess = '', 3000);
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
			videoConfigSuccess = 'Video model updated';
			setTimeout(() => videoConfigSuccess = '', 3000);
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

	// Cleanup settings functions
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
			const result = await api.post<CleanupConfig>('/settings/cleanup', cleanupConfig);
			cleanupConfig = result;
			cleanupSuccess = 'Settings saved successfully';
			setTimeout(() => cleanupSuccess = '', 3000);
		} catch (e: any) {
			cleanupError = e.detail || 'Failed to save settings';
		} finally {
			savingCleanup = false;
		}
	}

	async function loadCleanupPreview() {
		try {
			cleanupPreview = await api.get<CleanupPreview>('/settings/cleanup/preview');
			showCleanupPreview = true;
		} catch (e: any) {
			cleanupError = e.detail || 'Failed to load preview';
		}
	}

	async function runCleanupNow() {
		runningCleanup = true;
		cleanupError = '';
		cleanupSuccess = '';
		try {
			const result = await api.post<{
				success: boolean;
				images_deleted: number;
				videos_deleted: number;
				shared_files_deleted: number;
				bytes_freed: number;
				bytes_freed_formatted: string;
			}>('/settings/cleanup/run');
			const total = result.images_deleted + result.videos_deleted + result.shared_files_deleted;
			if (total > 0) {
				cleanupSuccess = `Cleanup complete: ${total} files deleted (${result.bytes_freed_formatted} freed)`;
			} else {
				cleanupSuccess = 'Cleanup complete: No files to delete';
			}
			showCleanupPreview = false;
			cleanupPreview = null;
			setTimeout(() => cleanupSuccess = '', 5000);
		} catch (e: any) {
			cleanupError = e.detail || 'Cleanup failed';
		} finally {
			runningCleanup = false;
		}
	}

	function formatIdleTime(seconds: number): string {
		if (seconds < 60) return `${Math.round(seconds)}s`;
		if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
		return `${Math.round(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
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
		claudeOAuthState = null;
		claudeAuthCode = '';
		error = '';
		try {
			const result = await api.post<{success: boolean, oauth_url?: string, state?: string, already_authenticated?: boolean, message: string, error?: string}>('/auth/claude/login', { force_reauth: forceReauth });
			if (result.already_authenticated) {
				await auth.checkAuth();
				claudeLoginLoading = false;
				return;
			}
			if (result.oauth_url && result.state) {
				claudeOAuthUrl = result.oauth_url;
				claudeOAuthState = result.state;
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
		if (!claudeOAuthState) {
			error = 'OAuth state missing. Please restart the login process.';
			return;
		}
		claudeCompletingLogin = true;
		error = '';
		try {
			const result = await api.post<{success: boolean, message: string, authenticated?: boolean, error?: string}>('/auth/claude/complete', {
				code: claudeAuthCode.trim(),
				state: claudeOAuthState
			});
			if (result.success && result.authenticated) {
				claudeOAuthUrl = null;
				claudeOAuthState = null;
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
		claudeOAuthState = null;
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
			} else if (showCleanupPreview) {
				showCleanupPreview = false;
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

		<!-- Modal Container - Full screen on mobile, large modal on desktop -->
		<div
			class="
				relative bg-card border border-border shadow-2xl
				flex flex-col overflow-hidden
				animate-modal-in
				w-full h-full
				sm:w-[95vw] sm:h-[90vh] sm:max-w-5xl sm:rounded-2xl
			"
			onclick={(e) => e.stopPropagation()}
		>
			<!-- Header -->
			<header class="shrink-0 px-4 sm:px-6 py-4 border-b border-border bg-card flex items-center justify-between gap-4">
				<div class="flex items-center gap-3 min-w-0">
					<!-- Mobile menu button -->
					<button
						class="sm:hidden p-2 -ml-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
						onclick={() => mobileMenuOpen = !mobileMenuOpen}
					>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
						</svg>
					</button>
					<div class="w-9 h-9 rounded-xl bg-primary/15 flex items-center justify-center shrink-0">
						<svg class="w-4.5 h-4.5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
						</svg>
					</div>
					<div class="min-w-0">
						<h2 id="settings-modal-title" class="text-lg font-semibold text-foreground truncate">Settings</h2>
						<p class="text-xs text-muted-foreground hidden sm:block">
							{categories.find(c => c.id === activeCategory)?.label} &rsaquo; {categories.find(c => c.id === activeCategory)?.sections.find(s => s.id === activeSection)?.label}
						</p>
					</div>
				</div>
				<button
					onclick={onClose}
					class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors shrink-0"
					aria-label="Close"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</header>

			<!-- Main content area -->
			<div class="flex-1 flex overflow-hidden relative">
				<!-- Mobile Menu Overlay -->
				{#if mobileMenuOpen}
					<div class="absolute inset-0 z-20 sm:hidden">
						<button class="absolute inset-0 bg-black/40" onclick={() => mobileMenuOpen = false}></button>
						<nav class="absolute left-0 top-0 bottom-0 w-64 bg-card border-r border-border p-4 overflow-y-auto animate-slide-in">
							<div class="space-y-6">
								{#each categories as category}
									<div>
										<button
											onclick={() => selectCategory(category.id)}
											class="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-all {activeCategory === category.id ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'}"
										>
											<svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d={category.icon} />
											</svg>
											<span class="font-medium">{category.label}</span>
										</button>
										{#if activeCategory === category.id}
											<div class="ml-8 mt-1 space-y-1">
												{#each category.sections as section}
													<button
														onclick={() => { selectSection(section.id); mobileMenuOpen = false; }}
														class="w-full text-left px-3 py-1.5 rounded-lg text-sm transition-all {activeSection === section.id ? 'text-primary font-medium' : 'text-muted-foreground hover:text-foreground'}"
													>
														{section.label}
													</button>
												{/each}
											</div>
										{/if}
									</div>
								{/each}
							</div>
						</nav>
					</div>
				{/if}

				<!-- Desktop Sidebar -->
				<nav class="hidden sm:flex w-56 shrink-0 border-r border-border bg-muted/30 flex-col p-4 overflow-y-auto">
					<div class="space-y-6">
						{#each categories as category}
							<div>
								<button
									onclick={() => selectCategory(category.id)}
									class="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-all {activeCategory === category.id ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'}"
								>
									<svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d={category.icon} />
									</svg>
									<span class="font-medium text-sm">{category.label}</span>
								</button>
								{#if activeCategory === category.id}
									<div class="ml-8 mt-1 space-y-0.5">
										{#each category.sections as section}
											<button
												onclick={() => selectSection(section.id)}
												class="w-full text-left px-3 py-1.5 rounded-lg text-sm transition-all {activeSection === section.id ? 'text-primary font-medium bg-primary/5' : 'text-muted-foreground hover:text-foreground hover:bg-muted/30'}"
											>
												{section.label}
											</button>
										{/each}
									</div>
								{/if}
							</div>
						{/each}
					</div>
				</nav>

				<!-- Content Area -->
				<div class="flex-1 overflow-y-auto p-4 sm:p-6">
					{#if error}
						<div class="bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 rounded-lg mb-4 flex items-center justify-between">
							<span>{error}</span>
							<button onclick={() => error = ''} class="hover:opacity-70 text-lg leading-none">&times;</button>
						</div>
					{/if}

					<!-- ACCOUNT > GENERAL -->
					{#if activeSection === 'general'}
						<div class="space-y-6 max-w-2xl">
							<div>
								<h3 class="text-lg font-semibold text-foreground">General Settings</h3>
								<p class="text-sm text-muted-foreground mt-1">Customize your experience.</p>
							</div>

							<!-- Theme -->
							<div class="settings-card">
								<h4 class="settings-card-title">
									<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										{#if $isDark}
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
										{:else}
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
										{/if}
									</svg>
									Appearance
								</h4>
								<div class="grid grid-cols-3 gap-3 mt-4">
									<button
										onclick={() => theme.setTheme('light')}
										class="theme-option {$themePreference === 'light' ? 'active' : ''}"
									>
										<div class="w-10 h-10 rounded-lg bg-white border border-gray-200 flex items-center justify-center shadow-sm">
											<svg class="w-5 h-5 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
											</svg>
										</div>
										<span class="text-sm font-medium">Light</span>
									</button>
									<button
										onclick={() => theme.setTheme('dark')}
										class="theme-option {$themePreference === 'dark' ? 'active' : ''}"
									>
										<div class="w-10 h-10 rounded-lg bg-gray-800 border border-gray-700 flex items-center justify-center shadow-sm">
											<svg class="w-5 h-5 text-blue-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
											</svg>
										</div>
										<span class="text-sm font-medium">Dark</span>
									</button>
									<button
										onclick={() => theme.setTheme('system')}
										class="theme-option {$themePreference === 'system' ? 'active' : ''}"
									>
										<div class="w-10 h-10 rounded-lg bg-gradient-to-br from-white to-gray-800 border border-gray-400 flex items-center justify-center shadow-sm">
											<svg class="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
											</svg>
										</div>
										<span class="text-sm font-medium">System</span>
									</button>
								</div>
							</div>

							<!-- Workspace -->
							<div class="settings-card">
								<h4 class="settings-card-title">
									<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
									</svg>
									Workspace
								</h4>
								{#if workspaceConfig === null}
									<div class="flex items-center gap-2 text-muted-foreground mt-3">
										<span class="inline-block animate-spin">&#9696;</span>
										<span>Loading...</span>
									</div>
								{:else if workspaceConfig?.is_local_mode}
									<div class="mt-4 space-y-3">
										<div class="flex gap-2">
											<input
												type="text"
												bind:value={workspacePath}
												oninput={handleWorkspaceInput}
												class="input font-mono text-sm flex-1"
												placeholder="/path/to/workspace"
											/>
											<button
												onclick={saveWorkspace}
												disabled={savingWorkspace || validatingWorkspace || !workspaceValidation?.valid || workspacePath === workspaceConfig?.workspace_path}
												class="btn btn-primary"
											>
												{savingWorkspace ? '...' : 'Save'}
											</button>
										</div>
										{#if workspaceSuccess}
											<p class="text-sm text-success">{workspaceSuccess}</p>
										{:else if workspaceError}
											<p class="text-sm text-destructive">{workspaceError}</p>
										{:else if validatingWorkspace}
											<p class="text-sm text-muted-foreground">Validating...</p>
										{:else if workspaceValidation && workspacePath !== workspaceConfig?.workspace_path}
											<p class="text-sm {workspaceValidation.valid ? 'text-success' : 'text-destructive'}">
												{workspaceValidation.valid ? (workspaceValidation.exists ? '✓ Directory exists' : '✓ Will be created') : 'Invalid path'}
											</p>
										{/if}
									</div>
								{:else}
									<div class="mt-4 flex items-start gap-3 p-3 bg-blue-500/10 rounded-lg">
										<span class="text-xl">🐳</span>
										<div>
											<p class="text-sm font-medium text-blue-400">Docker Mode</p>
											<p class="text-xs text-muted-foreground mt-1">Workspace: <code class="bg-muted px-1.5 py-0.5 rounded">{workspaceConfig?.workspace_path}</code></p>
										</div>
									</div>
								{/if}
							</div>
						</div>
					{/if}

					<!-- ACCOUNT > SECURITY -->
					{#if activeSection === 'security'}
						<div class="space-y-6 max-w-2xl">
							<div>
								<h3 class="text-lg font-semibold text-foreground">Security</h3>
								<p class="text-sm text-muted-foreground mt-1">Manage your account security.</p>
							</div>

							<!-- Change Password -->
							<div class="settings-card">
								<h4 class="settings-card-title">
									<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
									</svg>
									Change Password
								</h4>
								<div class="mt-4 space-y-3">
									<input type="password" bind:value={currentPassword} class="input" placeholder="Current password" autocomplete="current-password" />
									<input type="password" bind:value={newPassword} class="input" placeholder="New password (min 8 characters)" autocomplete="new-password" />
									<input type="password" bind:value={confirmPassword} class="input" placeholder="Confirm new password" autocomplete="new-password" />
									{#if passwordChangeError}
										<p class="text-sm text-destructive">{passwordChangeError}</p>
									{/if}
									{#if passwordChangeSuccess}
										<p class="text-sm text-success">{passwordChangeSuccess}</p>
									{/if}
									<button onclick={handlePasswordChange} class="btn btn-primary" disabled={changingPassword}>
										{changingPassword ? 'Changing...' : 'Change Password'}
									</button>
								</div>
							</div>

							<!-- 2FA -->
							<div class="settings-card">
								<h4 class="settings-card-title">
									<svg class="w-5 h-5 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
									</svg>
									Two-Factor Authentication
								</h4>
								<div class="mt-4">
									<TwoFactorSetup />
								</div>
							</div>

							<!-- Encryption Info -->
							<div class="settings-card">
								<div class="flex items-center gap-3">
									<div class="w-10 h-10 bg-success/15 rounded-lg flex items-center justify-center shrink-0">
										<svg class="w-5 h-5 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
										</svg>
									</div>
									<div>
										<h4 class="font-medium text-foreground">Encryption</h4>
										<p class="text-xs text-muted-foreground">Your API keys are encrypted at rest using your password.</p>
									</div>
								</div>
							</div>
						</div>
					{/if}

					<!-- ACCOUNT > AUTHENTICATION (Connections) -->
					{#if activeSection === 'authentication'}
						<div class="space-y-6 max-w-2xl">
							<div>
								<h3 class="text-lg font-semibold text-foreground">Connections</h3>
								<p class="text-sm text-muted-foreground mt-1">Connect external services.</p>
							</div>

							<div class="grid gap-4">
								<!-- Claude Code -->
								<div class="settings-card">
									<div class="flex items-center gap-3">
										<div class="w-10 h-10 bg-orange-500/15 rounded-lg flex items-center justify-center shrink-0">
											<svg class="w-5 h-5 text-orange-400" viewBox="0 0 24 24" fill="currentColor">
												<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
											</svg>
										</div>
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2">
												<span class="font-medium text-foreground">Claude Code</span>
												{#if $claudeAuthenticated}
													<span class="text-xs px-2 py-0.5 rounded-full bg-success/15 text-success">Connected</span>
												{:else}
													<span class="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">Not Connected</span>
												{/if}
											</div>
											<p class="text-xs text-muted-foreground">AI-powered code assistance</p>
										</div>
									</div>
									<div class="mt-4">
										{#if $claudeAuthenticated}
											<div class="flex gap-2">
												<button onclick={handleClaudeReconnect} disabled={claudeLoginLoading} class="btn btn-primary text-sm flex-1">
													{claudeLoginLoading ? 'Connecting...' : 'Reconnect'}
												</button>
												<button onclick={handleClaudeLogout} class="btn btn-secondary text-sm flex-1">Disconnect</button>
											</div>
										{:else if claudeOAuthUrl}
											<div class="space-y-2">
												<a href={claudeOAuthUrl} target="_blank" rel="noopener noreferrer" class="btn btn-primary text-sm w-full">
													1. Open Login Page
												</a>
												<div class="flex gap-2">
													<input type="text" bind:value={claudeAuthCode} placeholder="2. Paste code here" class="input text-sm flex-1" />
													<button onclick={completeClaudeLogin} disabled={claudeCompletingLogin || !claudeAuthCode.trim()} class="btn btn-primary text-sm">
														{claudeCompletingLogin ? '...' : 'OK'}
													</button>
												</div>
												<button onclick={cancelClaudeLogin} class="text-xs text-muted-foreground hover:text-foreground w-full">Cancel</button>
											</div>
										{:else}
											<button onclick={() => handleClaudeLogin()} disabled={claudeLoginLoading} class="btn btn-primary text-sm w-full">
												{claudeLoginLoading ? 'Starting...' : 'Connect Claude'}
											</button>
										{/if}
									</div>
								</div>

								<!-- GitHub -->
								<div class="settings-card">
									<div class="flex items-center gap-3">
										<div class="w-10 h-10 bg-white/10 rounded-lg flex items-center justify-center shrink-0">
											<svg class="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
												<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
											</svg>
										</div>
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2">
												<span class="font-medium text-foreground">GitHub</span>
												{#if $githubAuthenticated}
													<span class="text-xs px-2 py-0.5 rounded-full bg-success/15 text-success">Connected</span>
												{:else}
													<span class="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">Not Connected</span>
												{/if}
											</div>
											<p class="text-xs text-muted-foreground">{$githubAuthenticated && githubUser ? githubUser : 'Repository management'}</p>
										</div>
									</div>
									<div class="mt-4">
										{#if $githubAuthenticated}
											<button onclick={handleGitHubLogout} class="btn btn-secondary text-sm w-full">Disconnect</button>
										{:else}
											<div class="space-y-2">
												<input type="password" bind:value={githubToken} placeholder="Personal Access Token" class="input text-sm" />
												<button onclick={handleGitHubLogin} disabled={githubLoginLoading} class="btn btn-primary text-sm w-full">
													{githubLoginLoading ? 'Connecting...' : 'Connect GitHub'}
												</button>
												<p class="text-xs text-muted-foreground text-center">
													<a href="https://github.com/settings/tokens/new?scopes=repo,read:org,gist,workflow" target="_blank" class="text-primary hover:underline">Create token</a> with repo, read:org, gist, workflow
												</p>
											</div>
										{/if}
									</div>
								</div>
							</div>
						</div>
					{/if}

					<!-- API & USERS > USERS -->
					{#if activeSection === 'users'}
						<div class="space-y-6">
							<div class="flex items-center justify-between">
								<div>
									<h3 class="text-lg font-semibold text-foreground">API Users</h3>
									<p class="text-sm text-muted-foreground mt-1">Create users for programmatic access.</p>
								</div>
								<button onclick={openCreateForm} class="btn btn-primary text-sm">
									<svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
									</svg>
									Create
								</button>
							</div>

							{#if loading}
								<div class="text-center py-12">
									<div class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto"></div>
								</div>
							{:else if apiUsers.length === 0}
								<div class="settings-card text-center py-8">
									<div class="w-12 h-12 bg-muted rounded-full flex items-center justify-center mx-auto mb-3">
										<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
										</svg>
									</div>
									<p class="font-medium text-foreground">No API users yet</p>
									<p class="text-sm text-muted-foreground mt-1">Create one to enable external access.</p>
								</div>
							{:else}
								<div class="space-y-3">
									{#each apiUsers as user}
										<div class="settings-card">
											<div class="flex items-start justify-between gap-4">
												<div class="min-w-0 flex-1">
													<div class="flex items-center gap-2 flex-wrap">
														<span class="font-medium text-foreground">{user.name}</span>
														{#if user.username}
															<span class="text-xs px-2 py-0.5 rounded-full bg-primary/15 text-primary">@{user.username}</span>
														{/if}
														<span class="text-xs px-2 py-0.5 rounded-full {user.is_active ? 'bg-success/15 text-success' : 'bg-destructive/15 text-destructive'}">
															{user.is_active ? 'Active' : 'Inactive'}
														</span>
													</div>
													{#if user.description}
														<p class="text-sm text-muted-foreground mt-1">{user.description}</p>
													{/if}
													<div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground mt-2">
														<span>Profile: {profiles.find(p => p.id === user.profile_id)?.name || 'Any'}</span>
														<span>Last used: {formatDate(user.last_used_at)}</span>
													</div>
												</div>
												<div class="flex items-center gap-1 shrink-0">
													<button onclick={() => openEditForm(user)} class="icon-btn" title="Edit">
														<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
														</svg>
													</button>
													<button onclick={() => regenerateKey(user.id)} class="icon-btn" title="Regenerate Key">
														<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
														</svg>
													</button>
													<button onclick={() => toggleActive(user)} class="icon-btn" title={user.is_active ? 'Deactivate' : 'Activate'}>
														{#if user.is_active}
															<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
															</svg>
														{:else}
															<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
															</svg>
														{/if}
													</button>
													<button onclick={() => deleteUser(user.id)} class="icon-btn text-destructive" title="Delete">
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

							<!-- API Usage Hint -->
							<div class="settings-card">
								<h4 class="text-sm font-medium text-foreground mb-2">API Usage</h4>
								<pre class="bg-muted p-3 rounded-lg text-xs overflow-x-auto"><code>curl -H "Authorization: Bearer aih_your_key" ...</code></pre>
							</div>
						</div>
					{/if}

					<!-- API & USERS > RATE LIMITS -->
					{#if activeSection === 'rate-limits'}
						<div class="space-y-6">
							<div>
								<h3 class="text-lg font-semibold text-foreground">Rate Limits</h3>
								<p class="text-sm text-muted-foreground mt-1">Configure API rate limiting.</p>
							</div>
							<RateLimitManager />
						</div>
					{/if}

					<!-- AI MODELS > API KEYS -->
					{#if activeSection === 'api-keys'}
						<div class="space-y-6 max-w-2xl">
							<div>
								<h3 class="text-lg font-semibold text-foreground">API Keys</h3>
								<p class="text-sm text-muted-foreground mt-1">Configure provider API keys for AI features.</p>
							</div>

							<!-- OpenAI -->
							<div class="settings-card">
								<div class="flex items-center gap-3">
									<div class="w-10 h-10 bg-emerald-500/15 rounded-lg flex items-center justify-center shrink-0">
										<svg class="w-5 h-5 text-emerald-400" viewBox="0 0 24 24" fill="currentColor">
											<path d="M22.28 9.82a5.98 5.98 0 00-.52-4.91 6.05 6.05 0 00-6.51-2.9A6.07 6.07 0 004.98 4.18a5.98 5.98 0 00-4 2.9 6.05 6.05 0 00.74 7.1 5.98 5.98 0 00.51 4.91 6.05 6.05 0 006.51 2.9A5.98 5.98 0 0013.26 24a6.06 6.06 0 005.77-4.21 5.99 5.99 0 004-2.9 6.06 6.06 0 00-.75-7.07z"/>
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2">
											<span class="font-medium text-foreground">OpenAI</span>
											{#if openaiApiKeyMasked}
												<span class="text-xs px-2 py-0.5 rounded-full bg-success/15 text-success">Configured</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground">TTS, STT, GPT Image, Sora</p>
									</div>
								</div>
								<div class="mt-4 space-y-2">
									{#if openaiApiKeyMasked}
										<p class="text-xs text-muted-foreground font-mono">{openaiApiKeyMasked}</p>
									{/if}
									<div class="flex gap-2">
										<input type="password" bind:value={openaiApiKey} placeholder={openaiApiKeyMasked ? 'Enter new key...' : 'sk-...'} class="input text-sm font-mono flex-1" />
										<button onclick={saveOpenaiApiKey} disabled={savingOpenaiKey || !openaiApiKey.trim()} class="btn btn-primary text-sm">
											{savingOpenaiKey ? '...' : 'Save'}
										</button>
									</div>
									{#if openaiKeySuccess}
										<p class="text-xs text-success">{openaiKeySuccess}</p>
									{:else if openaiKeyError}
										<p class="text-xs text-destructive">{openaiKeyError}</p>
									{/if}
									<p class="text-xs text-muted-foreground">
										<a href="https://platform.openai.com/api-keys" target="_blank" class="text-primary hover:underline">Get API key</a>
									</p>
								</div>
							</div>

							<!-- Google Gemini -->
							<div class="settings-card">
								<div class="flex items-center gap-3">
									<div class="w-10 h-10 bg-blue-500/15 rounded-lg flex items-center justify-center shrink-0">
										<svg class="w-5 h-5 text-blue-400" viewBox="0 0 24 24" fill="currentColor">
											<path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
										</svg>
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2">
											<span class="font-medium text-foreground">Google Gemini</span>
											{#if imageApiKeyMasked}
												<span class="text-xs px-2 py-0.5 rounded-full bg-success/15 text-success">Configured</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground">Imagen, Veo, Gemini Flash</p>
									</div>
								</div>
								<div class="mt-4 space-y-2">
									{#if imageApiKeyMasked}
										<p class="text-xs text-muted-foreground font-mono">{imageApiKeyMasked}</p>
									{/if}
									<div class="flex gap-2">
										<input type="password" bind:value={imageApiKey} placeholder={imageApiKeyMasked ? 'Enter new key...' : 'AIza...'} class="input text-sm font-mono flex-1" />
										<button onclick={saveImageConfig} disabled={savingImageConfig || (!imageApiKey.trim() && !imageApiKeyMasked)} class="btn btn-primary text-sm">
											{savingImageConfig ? '...' : 'Save'}
										</button>
									</div>
									{#if imageConfigSuccess}
										<p class="text-xs text-success">{imageConfigSuccess}</p>
									{:else if imageConfigError}
										<p class="text-xs text-destructive">{imageConfigError}</p>
									{/if}
									<p class="text-xs text-muted-foreground">
										<a href="https://aistudio.google.com/apikey" target="_blank" class="text-primary hover:underline">Get API key</a>
									</p>
								</div>
							</div>
						</div>
					{/if}

					<!-- AI MODELS > MODELS -->
					{#if activeSection === 'models'}
						<div class="space-y-6">
							<div>
								<h3 class="text-lg font-semibold text-foreground">AI Models</h3>
								<p class="text-sm text-muted-foreground mt-1">Select default models for generation tasks.</p>
							</div>

							{#if audioConfigSuccess || imageConfigSuccess || videoConfigSuccess}
								<div class="bg-success/10 border border-success/30 text-success px-3 py-2 rounded-lg text-sm">
									{audioConfigSuccess || imageConfigSuccess || videoConfigSuccess}
								</div>
							{/if}

							<!-- Image Models -->
							<div class="settings-card">
								<h4 class="settings-card-title">
									<svg class="w-5 h-5 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
									</svg>
									Image Generation
								</h4>
								<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 mt-4">
									{#each imageModels as model}
										<button
											onclick={() => { selectedImageModel = model.id; updateImageModel(); }}
											disabled={!model.available || updatingImageModel}
											class="model-card {model.id === imageModel ? 'active' : ''} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
										>
											<div class="text-xs font-medium text-foreground leading-tight">{model.name}</div>
											<div class="mt-auto">
												<div class="text-xs text-primary font-medium">${model.price_per_image}/img</div>
												<div class="text-[10px] text-muted-foreground truncate">{model.provider_name}</div>
											</div>
										</button>
									{/each}
								</div>
							</div>

							<!-- Video Models -->
							<div class="settings-card">
								<h4 class="settings-card-title">
									<svg class="w-5 h-5 text-pink-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
									</svg>
									Video Generation
								</h4>
								<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 mt-4">
									{#each videoModels as model}
										<button
											onclick={() => { selectedVideoModel = model.id; saveVideoModel(); }}
											disabled={!model.available || savingVideoModel}
											class="model-card {model.id === videoModel ? 'active' : ''} {!model.available ? 'opacity-40 cursor-not-allowed' : ''}"
										>
											<div class="text-xs font-medium text-foreground leading-tight">{model.name}</div>
											<div class="mt-auto">
												<div class="text-xs text-primary font-medium">${model.price_per_second}/sec</div>
												<div class="text-[10px] text-muted-foreground truncate">{model.provider_name}</div>
											</div>
										</button>
									{/each}
								</div>
							</div>

							<!-- Audio Models -->
							<div class="grid gap-4 sm:grid-cols-2">
								<!-- STT -->
								<div class="settings-card">
									<h4 class="settings-card-title text-sm">
										<svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
										</svg>
										Speech-to-Text
									</h4>
									<div class="grid gap-2 mt-3">
										{#each sttModels as model}
											<button
												onclick={() => { selectedSttModel = model.id; saveSttModel(); }}
												disabled={!model.available || savingSttModel}
												class="model-card-sm {model.id === currentSttModel ? 'active' : ''} {!model.available ? 'opacity-40' : ''}"
											>
												<span class="text-xs font-medium">{model.name}</span>
												<span class="text-xs text-primary">{model.price_display}</span>
											</button>
										{/each}
									</div>
								</div>

								<!-- TTS -->
								<div class="settings-card">
									<h4 class="settings-card-title text-sm">
										<svg class="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
										</svg>
										Text-to-Speech
									</h4>
									<div class="grid gap-2 mt-3">
										{#each ttsModels as model}
											<button
												onclick={() => { selectedTtsModel = model.id; saveTtsModel(); }}
												disabled={!model.available || savingTtsModel}
												class="model-card-sm {model.id === currentTtsModel ? 'active' : ''} {!model.available ? 'opacity-40' : ''}"
											>
												<span class="text-xs font-medium">{model.name}</span>
												<span class="text-xs text-primary">{model.price_display}</span>
											</button>
										{/each}
									</div>
								</div>
							</div>
						</div>
					{/if}

					<!-- SYSTEM > NOTIFICATIONS -->
					{#if activeSection === 'notifications'}
						<div class="space-y-6 max-w-2xl">
							<div>
								<h3 class="text-lg font-semibold text-foreground">Notifications</h3>
								<p class="text-sm text-muted-foreground mt-1">Configure alerts and webhooks.</p>
							</div>

							<!-- Browser Notifications -->
							<div class="settings-card">
								<h4 class="settings-card-title">
									<svg class="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
									</svg>
									Browser Notifications
								</h4>
								<div class="mt-4">
									{#if !browserNotificationsSupported}
										<p class="text-sm text-amber-400">Not supported in this browser.</p>
									{:else if notificationPermission === 'denied'}
										<p class="text-sm text-destructive">Blocked. Enable in browser settings.</p>
									{:else}
										<div class="flex items-center justify-between">
											<div>
												<p class="text-sm font-medium text-foreground">Desktop notifications</p>
												<p class="text-xs text-muted-foreground">Get notified when sessions complete</p>
											</div>
											<button
												onclick={toggleBrowserNotifications}
												disabled={requestingPermission}
												class="toggle {browserNotificationsEnabled ? 'active' : ''}"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>
									{/if}
								</div>
							</div>

							<!-- Webhooks -->
							{#if $isAdmin}
								<div class="settings-card">
									<h4 class="settings-card-title">
										<svg class="w-5 h-5 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
										</svg>
										Webhooks
									</h4>
									<div class="mt-4">
										<WebhookManager />
									</div>
								</div>
							{/if}
						</div>
					{/if}

					<!-- SYSTEM > CLEANUP -->
					{#if activeSection === 'cleanup'}
						<div class="space-y-6">
							<div>
								<h3 class="text-lg font-semibold text-foreground">Background Cleanup</h3>
								<p class="text-sm text-muted-foreground mt-1">Automatic cleanup of sessions and files.</p>
							</div>

							{#if loadingCleanup}
								<div class="text-center py-12">
									<div class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto"></div>
								</div>
							{:else if cleanupConfig}
								<!-- Status -->
								<div class="settings-card flex items-center justify-between">
									<div class="flex items-center gap-3">
										<div class="w-10 h-10 rounded-lg flex items-center justify-center {cleanupStatus?.is_sleeping ? 'bg-amber-500/15' : 'bg-success/15'}">
											{#if cleanupStatus?.is_sleeping}
												<svg class="w-5 h-5 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
												</svg>
											{:else}
												<svg class="w-5 h-5 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
												</svg>
											{/if}
										</div>
										<div>
											<p class="font-medium text-foreground">{cleanupStatus?.is_sleeping ? 'Sleeping' : 'Active'}</p>
											<p class="text-xs text-muted-foreground">
												{#if cleanupStatus}Idle for {formatIdleTime(cleanupStatus.idle_seconds)}{/if}
											</p>
										</div>
									</div>
									<span class="text-xs text-muted-foreground">Every {cleanupConfig.cleanup_interval_minutes}m</span>
								</div>

								<!-- Sleep Mode -->
								<div class="settings-card">
									<div class="flex items-center justify-between">
										<div>
											<h4 class="font-medium text-foreground">Sleep Mode</h4>
											<p class="text-xs text-muted-foreground">Pause cleanup when inactive</p>
										</div>
										<button
											onclick={() => cleanupConfig && (cleanupConfig.sleep_mode_enabled = !cleanupConfig.sleep_mode_enabled)}
											class="toggle {cleanupConfig.sleep_mode_enabled ? 'active' : ''}"
										>
											<span class="toggle-knob"></span>
										</button>
									</div>
									{#if cleanupConfig.sleep_mode_enabled}
										<div class="grid grid-cols-2 gap-3 mt-4">
											<div>
												<label class="text-xs text-muted-foreground">Sleep after (min)</label>
												<input type="number" min="1" max="60" bind:value={cleanupConfig.sleep_timeout_minutes} class="input text-sm mt-1" />
											</div>
											<div>
												<label class="text-xs text-muted-foreground">Cleanup interval (min)</label>
												<input type="number" min="1" max="60" bind:value={cleanupConfig.cleanup_interval_minutes} class="input text-sm mt-1" />
											</div>
										</div>
									{/if}
								</div>

								<!-- File Cleanup -->
								<div class="settings-card">
									<h4 class="font-medium text-foreground mb-4">File Cleanup</h4>
									<div class="space-y-3">
										<!-- Images -->
										<div class="flex items-center justify-between p-3 bg-muted/50 rounded-lg">
											<div class="flex items-center gap-3">
												<span class="w-8 h-8 bg-green-500/15 rounded flex items-center justify-center">
													<svg class="w-4 h-4 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
													</svg>
												</span>
												<span class="text-sm font-medium">Images</span>
											</div>
											<div class="flex items-center gap-2">
												{#if cleanupConfig.cleanup_images_enabled}
													<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_images_max_age_days} class="input w-16 text-center text-sm" />
													<span class="text-xs text-muted-foreground">days</span>
												{/if}
												<button
													onclick={() => cleanupConfig && (cleanupConfig.cleanup_images_enabled = !cleanupConfig.cleanup_images_enabled)}
													class="toggle-sm {cleanupConfig.cleanup_images_enabled ? 'active' : ''}"
												>
													<span class="toggle-knob-sm"></span>
												</button>
											</div>
										</div>

										<!-- Videos -->
										<div class="flex items-center justify-between p-3 bg-muted/50 rounded-lg">
											<div class="flex items-center gap-3">
												<span class="w-8 h-8 bg-blue-500/15 rounded flex items-center justify-center">
													<svg class="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
													</svg>
												</span>
												<span class="text-sm font-medium">Videos</span>
											</div>
											<div class="flex items-center gap-2">
												{#if cleanupConfig.cleanup_videos_enabled}
													<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_videos_max_age_days} class="input w-16 text-center text-sm" />
													<span class="text-xs text-muted-foreground">days</span>
												{/if}
												<button
													onclick={() => cleanupConfig && (cleanupConfig.cleanup_videos_enabled = !cleanupConfig.cleanup_videos_enabled)}
													class="toggle-sm {cleanupConfig.cleanup_videos_enabled ? 'active' : ''}"
												>
													<span class="toggle-knob-sm"></span>
												</button>
											</div>
										</div>

										<!-- Shared Files -->
										<div class="flex items-center justify-between p-3 bg-muted/50 rounded-lg">
											<div class="flex items-center gap-3">
												<span class="w-8 h-8 bg-amber-500/15 rounded flex items-center justify-center">
													<svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
													</svg>
												</span>
												<span class="text-sm font-medium">Shared Files</span>
											</div>
											<div class="flex items-center gap-2">
												{#if cleanupConfig.cleanup_shared_files_enabled}
													<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_shared_files_max_age_days} class="input w-16 text-center text-sm" />
													<span class="text-xs text-muted-foreground">days</span>
												{/if}
												<button
													onclick={() => cleanupConfig && (cleanupConfig.cleanup_shared_files_enabled = !cleanupConfig.cleanup_shared_files_enabled)}
													class="toggle-sm {cleanupConfig.cleanup_shared_files_enabled ? 'active' : ''}"
												>
													<span class="toggle-knob-sm"></span>
												</button>
											</div>
										</div>
									</div>
								</div>

								<!-- Session Cleanup -->
								<div class="settings-card">
									<h4 class="font-medium text-foreground mb-4">Session Timeouts</h4>
									<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
										<div>
											<label class="text-xs text-muted-foreground">SDK Session (min)</label>
											<input type="number" min="5" max="480" bind:value={cleanupConfig.sdk_session_max_age_minutes} class="input text-sm mt-1" />
										</div>
										<div>
											<label class="text-xs text-muted-foreground">WebSocket (min)</label>
											<input type="number" min="1" max="60" bind:value={cleanupConfig.websocket_max_age_minutes} class="input text-sm mt-1" />
										</div>
										<div>
											<label class="text-xs text-muted-foreground">Sync Logs (hrs)</label>
											<input type="number" min="1" max="168" bind:value={cleanupConfig.sync_log_retention_hours} class="input text-sm mt-1" />
										</div>
									</div>
								</div>

								<!-- Messages & Actions -->
								{#if cleanupError}
									<div class="bg-destructive/10 border border-destructive/30 text-destructive px-4 py-3 rounded-lg text-sm">
										{cleanupError}
									</div>
								{/if}
								{#if cleanupSuccess}
									<div class="bg-success/10 border border-success/30 text-success px-4 py-3 rounded-lg text-sm">
										{cleanupSuccess}
									</div>
								{/if}

								<div class="flex gap-3">
									<button onclick={saveCleanupConfig} disabled={savingCleanup} class="btn btn-primary flex-1">
										{savingCleanup ? 'Saving...' : 'Save Settings'}
									</button>
									<button onclick={loadCleanupPreview} class="btn btn-secondary">
										Preview
									</button>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			</div>
		</div>
	</div>

	<!-- Create/Edit API User Modal -->
	{#if showCreateForm}
		<div class="fixed inset-0 bg-black/60 backdrop-blur-sm z-[60] flex items-center justify-center p-4">
			<div class="bg-card border border-border rounded-2xl w-full max-w-lg shadow-2xl animate-modal-in">
				<div class="p-4 border-b border-border flex items-center justify-between">
					<h3 class="text-lg font-semibold text-foreground">
						{editingUser ? 'Edit API User' : 'Create API User'}
					</h3>
					<button class="icon-btn" onclick={resetForm}>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<div class="p-4 space-y-4">
					{#if newlyCreatedKey}
						<div class="bg-success/10 border border-success/30 p-4 rounded-lg">
							<p class="text-success font-medium mb-2">API User created!</p>
							<p class="text-sm text-muted-foreground mb-3">Copy this key now - it won't be shown again:</p>
							<div class="flex items-center gap-2">
								<code class="flex-1 bg-muted px-3 py-2 rounded-lg text-sm break-all font-mono">{newlyCreatedKey}</code>
								<button onclick={() => copyToClipboard(newlyCreatedKey || '')} class="btn btn-secondary shrink-0">Copy</button>
							</div>
						</div>
						<button onclick={resetForm} class="btn btn-primary w-full">Done</button>
					{:else}
						<div>
							<label class="text-sm font-medium text-foreground">Name *</label>
							<input bind:value={formData.name} class="input mt-1" placeholder="My Application" />
						</div>
						<div>
							<label class="text-sm font-medium text-foreground">Description</label>
							<textarea bind:value={formData.description} class="input mt-1" rows="2" placeholder="Optional description"></textarea>
						</div>
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label class="text-sm font-medium text-foreground">Project</label>
								<select bind:value={formData.project_id} class="input mt-1">
									<option value="">Default</option>
									{#each projects as project}
										<option value={project.id}>{project.name}</option>
									{/each}
								</select>
							</div>
							<div>
								<label class="text-sm font-medium text-foreground">Profile</label>
								<select bind:value={formData.profile_id} class="input mt-1">
									<option value="">Any</option>
									{#each profiles as profile}
										<option value={profile.id}>{profile.name}</option>
									{/each}
								</select>
							</div>
						</div>
						<div class="flex items-center justify-between py-2">
							<div>
								<span class="text-sm font-medium text-foreground">Allow Web Login</span>
								<p class="text-xs text-muted-foreground">When disabled, API key access only</p>
							</div>
							<button
								onclick={() => formData.web_login_allowed = !formData.web_login_allowed}
								class="toggle {formData.web_login_allowed ? 'active' : ''}"
							>
								<span class="toggle-knob"></span>
							</button>
						</div>
						<div class="flex gap-2 pt-2">
							<button onclick={handleSubmit} class="btn btn-primary flex-1">
								{editingUser ? 'Save' : 'Create'}
							</button>
							<button onclick={resetForm} class="btn btn-secondary flex-1">Cancel</button>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}

	<!-- Cleanup Preview Modal -->
	{#if showCleanupPreview && cleanupPreview}
		<div class="fixed inset-0 bg-black/60 backdrop-blur-sm z-[60] flex items-center justify-center p-4">
			<div class="bg-card border border-border rounded-2xl w-full max-w-2xl shadow-2xl max-h-[80vh] flex flex-col animate-modal-in">
				<div class="p-4 border-b border-border flex items-center justify-between shrink-0">
					<h3 class="text-lg font-semibold text-foreground">Cleanup Preview</h3>
					<button class="icon-btn" onclick={() => showCleanupPreview = false}>
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>

				<div class="p-4 overflow-y-auto flex-1">
					{#if cleanupPreview.total_count === 0}
						<div class="text-center py-8 text-muted-foreground">
							<p>No files to clean up</p>
						</div>
					{:else}
						<div class="mb-4 p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg">
							<p class="text-amber-400 font-medium">
								{cleanupPreview.total_count} files ({cleanupPreview.total_bytes_formatted}) will be deleted
							</p>
						</div>

						<div class="space-y-4">
							{#if cleanupPreview.images.length > 0}
								<div>
									<h4 class="text-sm font-medium text-foreground mb-2">Images ({cleanupPreview.images.length})</h4>
									<div class="space-y-1 max-h-32 overflow-y-auto">
										{#each cleanupPreview.images as file}
											<div class="flex items-center justify-between text-xs px-2 py-1 bg-muted/30 rounded">
												<span class="truncate flex-1">{file.name}</span>
												<span class="text-muted-foreground ml-2">{file.age_days}d</span>
											</div>
										{/each}
									</div>
								</div>
							{/if}

							{#if cleanupPreview.videos.length > 0}
								<div>
									<h4 class="text-sm font-medium text-foreground mb-2">Videos ({cleanupPreview.videos.length})</h4>
									<div class="space-y-1 max-h-32 overflow-y-auto">
										{#each cleanupPreview.videos as file}
											<div class="flex items-center justify-between text-xs px-2 py-1 bg-muted/30 rounded">
												<span class="truncate flex-1">{file.name}</span>
												<span class="text-muted-foreground ml-2">{file.age_days}d</span>
											</div>
										{/each}
									</div>
								</div>
							{/if}

							{#if cleanupPreview.shared_files.length > 0}
								<div>
									<h4 class="text-sm font-medium text-foreground mb-2">Shared Files ({cleanupPreview.shared_files.length})</h4>
									<div class="space-y-1 max-h-32 overflow-y-auto">
										{#each cleanupPreview.shared_files as file}
											<div class="flex items-center justify-between text-xs px-2 py-1 bg-muted/30 rounded">
												<span class="truncate flex-1">{file.name}</span>
												<span class="text-muted-foreground ml-2">{file.age_days}d</span>
											</div>
										{/each}
									</div>
								</div>
							{/if}
						</div>
					{/if}
				</div>

				<div class="p-4 border-t border-border flex gap-3 shrink-0">
					{#if cleanupPreview.total_count > 0}
						<button onclick={runCleanupNow} disabled={runningCleanup} class="btn btn-primary flex-1">
							{runningCleanup ? 'Cleaning...' : `Delete ${cleanupPreview.total_count} Files`}
						</button>
					{/if}
					<button onclick={() => showCleanupPreview = false} class="btn btn-secondary {cleanupPreview.total_count === 0 ? 'flex-1' : ''}">
						{cleanupPreview.total_count === 0 ? 'Close' : 'Cancel'}
					</button>
				</div>
			</div>
		</div>
	{/if}
{/if}

<style>
	@keyframes modal-in {
		from {
			opacity: 0;
			transform: scale(0.96);
		}
		to {
			opacity: 1;
			transform: scale(1);
		}
	}

	@keyframes slide-in {
		from {
			transform: translateX(-100%);
		}
		to {
			transform: translateX(0);
		}
	}

	.animate-modal-in {
		animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.animate-slide-in {
		animation: slide-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	/* Settings Card */
	.settings-card {
		background-color: oklch(from var(--color-muted) l c h / 0.3);
		border-radius: 0.75rem;
		padding: 1rem;
		border: 1px solid oklch(from var(--color-border) l c h / 0.5);
	}

	.settings-card-title {
		font-weight: 500;
		color: var(--color-foreground);
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	/* Theme Options */
	.theme-option {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
		padding: 0.75rem;
		border-radius: 0.5rem;
		border: 2px solid var(--color-border);
		transition: all 0.2s;
	}
	.theme-option:hover {
		border-color: oklch(from var(--color-muted-foreground) l c h / 0.5);
	}
	.theme-option.active {
		border-color: var(--color-primary);
		background-color: oklch(from var(--color-primary) l c h / 0.1);
	}

	/* Model Cards */
	.model-card {
		height: 4rem;
		padding: 0.5rem;
		border-radius: 0.5rem;
		border: 1px solid var(--color-border);
		text-align: left;
		transition: all 0.2s;
		display: flex;
		flex-direction: column;
		justify-content: space-between;
	}
	.model-card:hover:not(:disabled) {
		border-color: oklch(from var(--color-primary) l c h / 0.5);
		background-color: oklch(from var(--color-muted) l c h / 0.5);
	}
	.model-card.active {
		border-color: var(--color-primary);
		background-color: oklch(from var(--color-primary) l c h / 0.1);
	}

	.model-card-sm {
		padding: 0.5rem 0.75rem;
		border-radius: 0.5rem;
		border: 1px solid var(--color-border);
		transition: all 0.2s;
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.model-card-sm:hover:not(:disabled) {
		border-color: oklch(from var(--color-primary) l c h / 0.5);
		background-color: oklch(from var(--color-muted) l c h / 0.5);
	}
	.model-card-sm.active {
		border-color: var(--color-primary);
		background-color: oklch(from var(--color-primary) l c h / 0.1);
	}

	/* Toggle Switch */
	.toggle {
		position: relative;
		width: 2.75rem;
		height: 1.5rem;
		background-color: var(--color-muted);
		border-radius: 9999px;
		cursor: pointer;
		transition: background-color 0.2s;
	}
	.toggle.active {
		background-color: var(--color-primary);
	}
	.toggle-knob {
		position: absolute;
		top: 0.125rem;
		left: 0.125rem;
		width: 1.25rem;
		height: 1.25rem;
		background-color: white;
		border-radius: 9999px;
		transition: transform 0.2s;
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
	}
	.toggle.active .toggle-knob {
		transform: translateX(1.25rem);
	}

	.toggle-sm {
		position: relative;
		width: 2.25rem;
		height: 1.25rem;
		background-color: var(--color-muted);
		border-radius: 9999px;
		cursor: pointer;
		transition: background-color 0.2s;
	}
	.toggle-sm.active {
		background-color: var(--color-primary);
	}
	.toggle-knob-sm {
		position: absolute;
		top: 0.125rem;
		left: 0.125rem;
		width: 1rem;
		height: 1rem;
		background-color: white;
		border-radius: 9999px;
		transition: transform 0.2s;
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
	}
	.toggle-sm.active .toggle-knob-sm {
		transform: translateX(1rem);
	}

	/* Icon Button */
	.icon-btn {
		padding: 0.375rem;
		color: var(--color-muted-foreground);
		border-radius: 0.5rem;
		transition: all 0.2s;
	}
	.icon-btn:hover {
		color: var(--color-foreground);
		background-color: var(--color-accent);
	}
</style>
