<script lang="ts">
	/**
	 * SettingsCard - Settings management in card format
	 *
	 * Categories:
	 * - Account: General settings, Security, Authentication
	 * - API & Users: API Users management, Rate Limits
	 * - AI Models: API Keys, Image/Video/Audio model selection
	 * - System: Notifications, Background Cleanup
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
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
	import WebhookManager from '$lib/components/WebhookManager.svelte';
	import TwoFactorSetup from '$lib/components/TwoFactorSetup.svelte';
	import RateLimitManager from '$lib/components/RateLimitManager.svelte';
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
		onMinimize: () => void;
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
		onMinimize,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

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

	// Integration settings
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

	// Extended TTS settings
	let selectedTtsVoice = $state('alloy');
	let currentTtsVoice = $state('alloy');
	let ttsSpeed = $state(1.0);
	let currentTtsSpeed = $state(1.0);
	let ttsOutputFormat = $state('mp3');
	let currentTtsOutputFormat = $state('mp3');
	let savingTtsSettings = $state(false);

	// Helper function to get available voices for selected TTS model
	function getAvailableVoices(): TTSVoice[] {
		const model = ALL_TTS_MODELS.find(m => m.id === selectedTtsModel);
		return model?.voices || [];
	}

	// Helper to group models by provider
	function groupImageModelsByProvider() {
		const groups: Record<string, AIImageModel[]> = {};
		for (const model of ALL_IMAGE_MODELS) {
			const providerName = PROVIDER_DISPLAY_NAMES[model.provider] || model.provider;
			if (!groups[providerName]) groups[providerName] = [];
			groups[providerName].push(model);
		}
		return groups;
	}

	function groupVideoModelsByProvider() {
		const groups: Record<string, AIVideoModel[]> = {};
		for (const model of ALL_VIDEO_MODELS) {
			const providerName = PROVIDER_DISPLAY_NAMES[model.provider] || model.provider;
			if (!groups[providerName]) groups[providerName] = [];
			groups[providerName].push(model);
		}
		return groups;
	}

	function groupTtsModelsByProvider() {
		const groups: Record<string, TTSModel[]> = {};
		for (const model of ALL_TTS_MODELS) {
			const providerName = PROVIDER_DISPLAY_NAMES[model.provider] || model.provider;
			if (!groups[providerName]) groups[providerName] = [];
			groups[providerName].push(model);
		}
		return groups;
	}

	function groupSttModelsByProvider() {
		const groups: Record<string, STTModel[]> = {};
		for (const model of ALL_STT_MODELS) {
			const providerName = PROVIDER_DISPLAY_NAMES[model.provider] || model.provider;
			if (!groups[providerName]) groups[providerName] = [];
			groups[providerName].push(model);
		}
		return groups;
	}

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
				tts_voice: string | null;
				tts_speed: number | null;
				tts_output_format: string | null;
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
			// Extended TTS settings
			currentTtsVoice = settings.tts_voice || 'alloy';
			selectedTtsVoice = currentTtsVoice;
			currentTtsSpeed = settings.tts_speed || 1.0;
			ttsSpeed = currentTtsSpeed;
			currentTtsOutputFormat = settings.tts_output_format || 'mp3';
			ttsOutputFormat = currentTtsOutputFormat;
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
			// Reset voice to default when changing models
			const model = ALL_TTS_MODELS.find(m => m.id === selectedTtsModel);
			if (model && model.voices.length > 0) {
				selectedTtsVoice = model.voices[0].id;
			}
			audioConfigSuccess = 'TTS model updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: any) {
			audioConfigError = e.detail || 'Failed to update TTS model';
		} finally {
			savingTtsModel = false;
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
				output_format: ttsOutputFormat
			});
			currentTtsModel = selectedTtsModel;
			currentTtsVoice = selectedTtsVoice;
			currentTtsSpeed = ttsSpeed;
			currentTtsOutputFormat = ttsOutputFormat;
			audioConfigSuccess = 'TTS settings updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: any) {
			audioConfigError = e.detail || 'Failed to update TTS settings';
		} finally {
			savingTtsSettings = false;
		}
	}

	async function saveSttModel() {
		if (!selectedSttModel || selectedSttModel === currentSttModel) return;
		savingSttModel = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean, model: string}>('/settings/integrations/stt', {
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
			const result = await api.post<{success: boolean, message: string, error?: string}>('/auth/claude/complete', {
				code: claudeAuthCode,
				state: claudeOAuthState
			});
			if (result.success) {
				claudeOAuthUrl = null;
				claudeOAuthState = null;
				claudeAuthCode = '';
				await auth.checkAuth();
			} else {
				error = result.error || result.message;
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
		if (!currentPassword || !newPassword || !confirmPassword) {
			passwordChangeError = 'Please fill in all fields';
			return;
		}
		if (newPassword.length < 8) {
			passwordChangeError = 'New password must be at least 8 characters';
			return;
		}
		if (newPassword !== confirmPassword) {
			passwordChangeError = 'Passwords do not match';
			return;
		}

		changingPassword = true;
		passwordChangeError = '';
		passwordChangeSuccess = '';
		try {
			await changePassword(currentPassword, newPassword);
			passwordChangeSuccess = 'Password changed successfully';
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

	// API User functions
	function openCreateForm() {
		editingUser = null;
		formData = { name: '', description: '', project_id: '', profile_id: '', web_login_allowed: true };
		showCreateForm = true;
	}

	function openEditForm(user: ApiUser) {
		editingUser = user;
		formData = {
			name: user.name,
			description: user.description || '',
			project_id: user.project_id || '',
			profile_id: user.profile_id || '',
			web_login_allowed: user.web_login_allowed
		};
		showCreateForm = true;
	}

	function resetForm() {
		showCreateForm = false;
		editingUser = null;
		newlyCreatedKey = null;
		regeneratedKey = null;
		formData = { name: '', description: '', project_id: '', profile_id: '', web_login_allowed: true };
	}

	async function saveApiUser() {
		if (!formData.name.trim()) {
			error = 'Name is required';
			return;
		}
		try {
			if (editingUser) {
				await api.put(`/api-users/${editingUser.id}`, formData);
				await loadData();
				resetForm();
			} else {
				const result = await api.post<ApiUserWithKey>('/api-users', formData);
				newlyCreatedKey = result.api_key;
				await loadData();
			}
		} catch (e: any) {
			error = e.detail || 'Failed to save API user';
		}
	}

	async function deleteUser(userId: string) {
		if (!confirm('Delete this API user?')) return;
		try {
			await api.delete(`/api-users/${userId}`);
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete user';
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
		if (!confirm('Regenerate API key? The old key will stop working.')) return;
		try {
			const result = await api.post<{api_key: string}>(`/api-users/${userId}/regenerate-key`);
			regeneratedKey = result.api_key;
		} catch (e: any) {
			error = e.detail || 'Failed to regenerate key';
		}
	}

	function copyToClipboard(text: string) {
		navigator.clipboard.writeText(text);
	}

	function formatDate(date: string | null): string {
		if (!date) return 'Never';
		return new Date(date).toLocaleDateString();
	}
</script>

{#if mobile}
	<div class="settings-card-content mobile">
		<div class="mobile-header">
			<button onclick={() => mobileMenuOpen = !mobileMenuOpen} class="menu-toggle">
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
				</svg>
				<span>{categories.find(c => c.id === activeCategory)?.label} / {categories.find(c => c.id === activeCategory)?.sections.find(s => s.id === activeSection)?.label}</span>
			</button>
		</div>

		{#if mobileMenuOpen}
			<div class="mobile-menu">
				{#each categories as cat}
					<div class="mobile-category">
						<button onclick={() => selectCategory(cat.id)} class="category-btn {activeCategory === cat.id ? 'active' : ''}">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={cat.icon} />
							</svg>
							{cat.label}
						</button>
						{#if activeCategory === cat.id}
							<div class="mobile-sections">
								{#each cat.sections as section}
									<button
										onclick={() => { selectSection(section.id); mobileMenuOpen = false; }}
										class="section-btn {activeSection === section.id ? 'active' : ''}"
									>
										{section.label}
									</button>
								{/each}
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}

		<div class="mobile-content">
			{@render settingsContent()}
		</div>
	</div>
{:else}
	<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="settings-card-content">
			<!-- Sidebar -->
			<nav class="settings-sidebar">
				{#each categories as cat}
					<div class="sidebar-category">
						<button
							onclick={() => selectCategory(cat.id)}
							class="category-header {activeCategory === cat.id ? 'active' : ''}"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={cat.icon} />
							</svg>
							<span>{cat.label}</span>
						</button>
						{#if activeCategory === cat.id}
							<div class="sidebar-sections">
								{#each cat.sections as section}
									<button
										onclick={() => selectSection(section.id)}
										class="section-btn {activeSection === section.id ? 'active' : ''}"
									>
										{section.label}
									</button>
								{/each}
							</div>
						{/if}
					</div>
				{/each}
			</nav>

			<!-- Content -->
			<div class="settings-content">
				{@render settingsContent()}
			</div>
		</div>
	</BaseCard>
{/if}

{#snippet settingsContent()}
	{#if error}
		<div class="error-banner">
			<span>{error}</span>
			<button onclick={() => error = ''} class="close-btn">&times;</button>
		</div>
	{/if}

	<!-- ACCOUNT > GENERAL -->
	{#if activeSection === 'general'}
		<div class="section-content">
			<div class="section-header">
				<h3>General Settings</h3>
				<p>Customize your experience.</p>
			</div>

			<!-- Theme -->
			<div class="settings-group">
				<h4>Appearance</h4>
				<div class="theme-options">
					<button onclick={() => theme.setTheme('light')} class="theme-btn {$themePreference === 'light' ? 'active' : ''}">
						<div class="theme-preview light">☀️</div>
						<span>Light</span>
					</button>
					<button onclick={() => theme.setTheme('dark')} class="theme-btn {$themePreference === 'dark' ? 'active' : ''}">
						<div class="theme-preview dark">🌙</div>
						<span>Dark</span>
					</button>
					<button onclick={() => theme.setTheme('system')} class="theme-btn {$themePreference === 'system' ? 'active' : ''}">
						<div class="theme-preview system">💻</div>
						<span>System</span>
					</button>
				</div>
			</div>

			<!-- Workspace -->
			<div class="settings-group">
				<h4>Workspace</h4>
				{#if workspaceConfig === null}
					<p class="text-muted">Loading...</p>
				{:else if workspaceConfig?.is_local_mode}
					<div class="input-row">
						<input
							type="text"
							bind:value={workspacePath}
							oninput={handleWorkspaceInput}
							class="input flex-1 font-mono text-sm"
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
						<p class="text-success text-sm">{workspaceSuccess}</p>
					{:else if workspaceError}
						<p class="text-error text-sm">{workspaceError}</p>
					{:else if validatingWorkspace}
						<p class="text-muted text-sm">Validating...</p>
					{:else if workspaceValidation && workspacePath !== workspaceConfig?.workspace_path}
						<p class="text-sm {workspaceValidation.valid ? 'text-success' : 'text-error'}">
							{workspaceValidation.valid ? (workspaceValidation.exists ? '✓ Directory exists' : '✓ Will be created') : 'Invalid path'}
						</p>
					{/if}
				{:else}
					<div class="docker-info">
						<span>🐳</span>
						<div>
							<p class="font-medium">Docker Mode</p>
							<p class="text-muted text-sm">Workspace: <code>{workspaceConfig?.workspace_path}</code></p>
						</div>
					</div>
				{/if}
			</div>
		</div>
	{/if}

	<!-- ACCOUNT > SECURITY -->
	{#if activeSection === 'security'}
		<div class="section-content">
			<div class="section-header">
				<h3>Security</h3>
				<p>Manage your account security.</p>
			</div>

			<!-- Change Password -->
			<div class="settings-group">
				<h4>Change Password</h4>
				<div class="form-stack">
					<input type="password" bind:value={currentPassword} class="input" placeholder="Current password" autocomplete="current-password" />
					<input type="password" bind:value={newPassword} class="input" placeholder="New password (min 8 characters)" autocomplete="new-password" />
					<input type="password" bind:value={confirmPassword} class="input" placeholder="Confirm new password" autocomplete="new-password" />
					{#if passwordChangeError}
						<p class="text-error text-sm">{passwordChangeError}</p>
					{/if}
					{#if passwordChangeSuccess}
						<p class="text-success text-sm">{passwordChangeSuccess}</p>
					{/if}
					<button onclick={handlePasswordChange} class="btn btn-primary" disabled={changingPassword}>
						{changingPassword ? 'Changing...' : 'Change Password'}
					</button>
				</div>
			</div>

			<!-- 2FA -->
			<div class="settings-group">
				<h4>Two-Factor Authentication</h4>
				<TwoFactorSetup />
			</div>

			<!-- Encryption Info -->
			<div class="info-card success">
				<span class="icon">🔒</span>
				<div>
					<h4>Encryption</h4>
					<p>Your API keys are encrypted at rest using your password.</p>
				</div>
			</div>
		</div>
	{/if}

	<!-- ACCOUNT > AUTHENTICATION (Connections) -->
	{#if activeSection === 'authentication'}
		<div class="section-content">
			<div class="section-header">
				<h3>Connections</h3>
				<p>Connect external services.</p>
			</div>

			<!-- Claude Code -->
			<div class="connection-card">
				<div class="connection-header">
					<div class="connection-icon orange">🤖</div>
					<div class="connection-info">
						<div class="connection-title">
							<span>Claude Code</span>
							{#if $claudeAuthenticated}
								<span class="badge success">Connected</span>
							{:else}
								<span class="badge muted">Not Connected</span>
							{/if}
						</div>
						<p class="text-muted text-sm">AI-powered code assistance</p>
					</div>
				</div>
				<div class="connection-actions">
					{#if $claudeAuthenticated}
						<button onclick={handleClaudeReconnect} disabled={claudeLoginLoading} class="btn btn-primary flex-1">
							{claudeLoginLoading ? 'Connecting...' : 'Reconnect'}
						</button>
						<button onclick={handleClaudeLogout} class="btn btn-secondary flex-1">Disconnect</button>
					{:else if claudeOAuthUrl}
						<div class="oauth-flow">
							<a href={claudeOAuthUrl} target="_blank" rel="noopener noreferrer" class="btn btn-primary w-full">
								1. Open Login Page
							</a>
							<div class="input-row">
								<input type="text" bind:value={claudeAuthCode} placeholder="2. Paste code here" class="input flex-1" />
								<button onclick={completeClaudeLogin} disabled={claudeCompletingLogin || !claudeAuthCode.trim()} class="btn btn-primary">
									{claudeCompletingLogin ? '...' : 'OK'}
								</button>
							</div>
							<button onclick={cancelClaudeLogin} class="text-muted text-sm hover:underline">Cancel</button>
						</div>
					{:else}
						<button onclick={() => handleClaudeLogin()} disabled={claudeLoginLoading} class="btn btn-primary w-full">
							{claudeLoginLoading ? 'Starting...' : 'Connect Claude'}
						</button>
					{/if}
				</div>
			</div>

			<!-- GitHub -->
			<div class="connection-card">
				<div class="connection-header">
					<div class="connection-icon">🐙</div>
					<div class="connection-info">
						<div class="connection-title">
							<span>GitHub</span>
							{#if $githubAuthenticated}
								<span class="badge success">Connected</span>
							{:else}
								<span class="badge muted">Not Connected</span>
							{/if}
						</div>
						<p class="text-muted text-sm">{$githubAuthenticated && githubUser ? githubUser : 'Repository management'}</p>
					</div>
				</div>
				<div class="connection-actions">
					{#if $githubAuthenticated}
						<button onclick={handleGitHubLogout} class="btn btn-secondary w-full">Disconnect</button>
					{:else}
						<input type="password" bind:value={githubToken} placeholder="Personal Access Token" class="input" />
						<button onclick={handleGitHubLogin} disabled={githubLoginLoading} class="btn btn-primary w-full">
							{githubLoginLoading ? 'Connecting...' : 'Connect GitHub'}
						</button>
						<p class="text-muted text-sm text-center">
							<a href="https://github.com/settings/tokens/new?scopes=repo,read:org,gist,workflow" target="_blank" class="text-primary hover:underline">Create token</a>
						</p>
					{/if}
				</div>
			</div>
		</div>
	{/if}

	<!-- API & USERS > USERS -->
	{#if activeSection === 'users'}
		<div class="section-content">
			<div class="section-header">
				<h3>API Users</h3>
				<p>Create users for programmatic access.</p>
				<button onclick={openCreateForm} class="btn btn-primary btn-sm">+ Create</button>
			</div>

			{#if loading}
				<div class="loading">Loading...</div>
			{:else if apiUsers.length === 0}
				<div class="empty-state">
					<p>No API users yet</p>
					<p class="text-muted">Create one to enable external access.</p>
				</div>
			{:else}
				<div class="user-list">
					{#each apiUsers as user}
						<div class="user-card">
							<div class="user-info">
								<div class="user-header">
									<span class="user-name">{user.name}</span>
									{#if user.username}
										<span class="badge primary">@{user.username}</span>
									{/if}
									<span class="badge {user.is_active ? 'success' : 'error'}">{user.is_active ? 'Active' : 'Inactive'}</span>
								</div>
								{#if user.description}
									<p class="user-desc">{user.description}</p>
								{/if}
								<div class="user-meta">
									<span>Profile: {profiles.find(p => p.id === user.profile_id)?.name || 'Any'}</span>
									<span>Last used: {formatDate(user.last_used_at)}</span>
								</div>
							</div>
							<div class="user-actions">
								<button onclick={() => openEditForm(user)} class="icon-btn" title="Edit">✏️</button>
								<button onclick={() => regenerateKey(user.id)} class="icon-btn" title="Regenerate Key">🔄</button>
								<button onclick={() => toggleActive(user)} class="icon-btn" title={user.is_active ? 'Deactivate' : 'Activate'}>
									{user.is_active ? '⏸️' : '▶️'}
								</button>
								<button onclick={() => deleteUser(user.id)} class="icon-btn danger" title="Delete">🗑️</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}

			{#if regeneratedKey}
				<div class="key-display">
					<p class="font-medium">New API Key (copy now):</p>
					<div class="input-row">
						<code class="flex-1">{regeneratedKey}</code>
						<button onclick={() => { copyToClipboard(regeneratedKey || ''); regeneratedKey = null; }} class="btn btn-primary">Copy</button>
					</div>
				</div>
			{/if}
		</div>
	{/if}

	<!-- API & USERS > RATE LIMITS -->
	{#if activeSection === 'rate-limits'}
		<div class="section-content">
			<div class="section-header">
				<h3>Rate Limits</h3>
				<p>Configure API rate limiting.</p>
			</div>
			<RateLimitManager />
		</div>
	{/if}

	<!-- AI MODELS > API KEYS -->
	{#if activeSection === 'api-keys'}
		<div class="section-content">
			<div class="section-header">
				<h3>API Keys</h3>
				<p>Configure provider API keys for AI features.</p>
			</div>

			<!-- OpenAI -->
			<div class="api-key-card">
				<div class="api-key-header">
					<div class="api-icon openai">🤖</div>
					<div>
						<div class="api-title">
							<span>OpenAI</span>
							{#if openaiApiKeyMasked}
								<span class="badge success">Configured</span>
							{/if}
						</div>
						<p class="text-muted text-sm">TTS, STT, GPT Image, Sora</p>
					</div>
				</div>
				<div class="api-key-form">
					{#if openaiApiKeyMasked}
						<p class="text-muted text-sm font-mono">{openaiApiKeyMasked}</p>
					{/if}
					<div class="input-row">
						<input type="password" bind:value={openaiApiKey} placeholder={openaiApiKeyMasked ? 'Enter new key...' : 'sk-...'} class="input flex-1 font-mono" />
						<button onclick={saveOpenaiApiKey} disabled={savingOpenaiKey || !openaiApiKey.trim()} class="btn btn-primary">
							{savingOpenaiKey ? '...' : 'Save'}
						</button>
					</div>
					{#if openaiKeySuccess}
						<p class="text-success text-sm">{openaiKeySuccess}</p>
					{:else if openaiKeyError}
						<p class="text-error text-sm">{openaiKeyError}</p>
					{/if}
				</div>
			</div>

			<!-- Google Gemini -->
			<div class="api-key-card">
				<div class="api-key-header">
					<div class="api-icon google">🔷</div>
					<div>
						<div class="api-title">
							<span>Google Gemini</span>
							{#if imageApiKeyMasked}
								<span class="badge success">Configured</span>
							{/if}
						</div>
						<p class="text-muted text-sm">Imagen, Veo, Gemini Flash</p>
					</div>
				</div>
				<div class="api-key-form">
					{#if imageApiKeyMasked}
						<p class="text-muted text-sm font-mono">{imageApiKeyMasked}</p>
					{/if}
					<div class="input-row">
						<input type="password" bind:value={imageApiKey} placeholder={imageApiKeyMasked ? 'Enter new key...' : 'AIza...'} class="input flex-1 font-mono" />
						<button onclick={saveImageConfig} disabled={savingImageConfig || (!imageApiKey.trim() && !imageApiKeyMasked)} class="btn btn-primary">
							{savingImageConfig ? '...' : 'Save'}
						</button>
					</div>
					{#if imageConfigSuccess}
						<p class="text-success text-sm">{imageConfigSuccess}</p>
					{:else if imageConfigError}
						<p class="text-error text-sm">{imageConfigError}</p>
					{/if}
				</div>
			</div>
		</div>
	{/if}

	<!-- AI MODELS > MODELS -->
	{#if activeSection === 'models'}
		<div class="section-content models-section">
			<div class="section-header">
				<h3>AI Models</h3>
				<p>Select default models for generation tasks.</p>
			</div>

			{#if audioConfigSuccess || imageConfigSuccess || videoConfigSuccess}
				<div class="success-banner">{audioConfigSuccess || imageConfigSuccess || videoConfigSuccess}</div>
			{/if}
			{#if audioConfigError || imageConfigError || videoConfigError}
				<div class="error-banner">
					<span>{audioConfigError || imageConfigError || videoConfigError}</span>
					<button onclick={() => { audioConfigError = ''; imageConfigError = ''; videoConfigError = ''; }} class="close-btn">&times;</button>
				</div>
			{/if}

			<!-- Image Generation Models -->
			<div class="settings-group">
				<h4>Image Generation</h4>
				<p class="group-description">Select the default model for generating images.</p>

				{#each Object.entries(groupImageModelsByProvider()) as [provider, models]}
					<div class="provider-section">
						<div class="provider-header">{provider}</div>
						<div class="model-cards">
							{#each models as model}
								<button
									onclick={() => { selectedImageModel = model.id; updateImageModel(); }}
									disabled={updatingImageModel}
									class="model-card {model.id === imageModel ? 'active' : ''} {model.deprecated ? 'deprecated' : ''}"
								>
									<div class="model-card-header">
										<span class="model-card-name">{model.displayName}</span>
										{#if model.deprecated}
											<span class="badge warning">Deprecated</span>
										{/if}
									</div>
									<p class="model-card-desc">{model.description}</p>
									<div class="model-card-capabilities">
										{#if model.capabilities.editing}
											<span class="capability-badge">Editing</span>
										{/if}
										{#if model.capabilities.inpainting}
											<span class="capability-badge">Inpainting</span>
										{/if}
										{#if model.capabilities.referenceImages}
											<span class="capability-badge">References</span>
										{/if}
										{#if model.capabilities.textRendering !== 'none'}
											<span class="capability-badge text-{model.capabilities.textRendering}">
												Text: {model.capabilities.textRendering}
											</span>
										{/if}
									</div>
									<div class="model-card-footer">
										<span class="model-card-price">${model.pricePerImage.toFixed(2)}/image</span>
										<span class="model-card-resolution">{model.maxOutputSize}</span>
									</div>
								</button>
							{/each}
						</div>
					</div>
				{/each}
			</div>

			<!-- Video Generation Models -->
			<div class="settings-group">
				<h4>Video Generation</h4>
				<p class="group-description">Select the default model for generating videos.</p>

				{#each Object.entries(groupVideoModelsByProvider()) as [provider, models]}
					<div class="provider-section">
						<div class="provider-header">{provider}</div>
						<div class="model-cards">
							{#each models as model}
								<button
									onclick={() => { selectedVideoModel = model.id; saveVideoModel(); }}
									disabled={savingVideoModel}
									class="model-card {model.id === videoModel ? 'active' : ''}"
								>
									<div class="model-card-header">
										<span class="model-card-name">{model.displayName}</span>
									</div>
									<p class="model-card-desc">{model.description}</p>
									<div class="model-card-capabilities">
										{#if model.capabilities.nativeAudio}
											<span class="capability-badge audio">Audio</span>
										{/if}
										{#if model.capabilities.extension}
											<span class="capability-badge">Extend</span>
										{/if}
										{#if model.capabilities.frameBridging}
											<span class="capability-badge">Frame Bridge</span>
										{/if}
										{#if model.capabilities.imageToVideo}
											<span class="capability-badge">Image-to-Video</span>
										{/if}
									</div>
									<div class="model-card-footer">
										<span class="model-card-price">${model.pricePerSecond.toFixed(2)}/sec</span>
										<span class="model-card-resolution">Max {model.maxDuration}s</span>
									</div>
								</button>
							{/each}
						</div>
					</div>
				{/each}
			</div>

			<!-- Text-to-Speech Models -->
			<div class="settings-group">
				<h4>Text-to-Speech</h4>
				<p class="group-description">Configure speech synthesis settings.</p>

				{#each Object.entries(groupTtsModelsByProvider()) as [provider, models]}
					<div class="provider-section">
						<div class="provider-header">{provider}</div>
						<div class="model-cards tts-cards">
							{#each models as model}
								<button
									onclick={() => { selectedTtsModel = model.id; }}
									class="model-card {model.id === selectedTtsModel ? 'active' : ''}"
								>
									<div class="model-card-header">
										<span class="model-card-name">{model.displayName}</span>
									</div>
									<p class="model-card-desc">{model.description}</p>
									<div class="model-card-capabilities">
										{#if model.capabilities.steerability}
											<span class="capability-badge highlight">Steerable</span>
										{/if}
										{#if model.capabilities.streaming}
											<span class="capability-badge">Streaming</span>
										{/if}
										{#if model.capabilities.emotionControl}
											<span class="capability-badge">Emotion</span>
										{/if}
										{#if model.capabilities.multiSpeaker}
											<span class="capability-badge">Multi-Speaker</span>
										{/if}
									</div>
									<div class="model-card-footer">
										<span class="model-card-price">${model.pricePerMillion}/1M chars</span>
									</div>
								</button>
							{/each}
						</div>
					</div>
				{/each}

				<!-- TTS Settings -->
				<div class="tts-settings">
					<div class="tts-setting-row">
						<label>Voice</label>
						<select bind:value={selectedTtsVoice} class="input">
							{#each getAvailableVoices() as voice}
								<option value={voice.id}>{voice.name} {voice.gender ? `(${voice.gender})` : ''}</option>
							{/each}
							{#if getAvailableVoices().length === 0}
								<option value="alloy">Alloy (default)</option>
								<option value="echo">Echo</option>
								<option value="fable">Fable</option>
								<option value="onyx">Onyx</option>
								<option value="nova">Nova</option>
								<option value="shimmer">Shimmer</option>
							{/if}
						</select>
					</div>
					<div class="tts-setting-row">
						<label>Speed ({ttsSpeed.toFixed(2)}x)</label>
						<input type="range" min="0.25" max="4.0" step="0.05" bind:value={ttsSpeed} class="slider" />
					</div>
					<div class="tts-setting-row">
						<label>Output Format</label>
						<select bind:value={ttsOutputFormat} class="input">
							<option value="mp3">MP3</option>
							<option value="wav">WAV</option>
							<option value="opus">Opus</option>
							<option value="aac">AAC</option>
							<option value="flac">FLAC</option>
						</select>
					</div>
					<button
						onclick={saveTtsSettings}
						disabled={savingTtsSettings}
						class="btn btn-primary"
					>
						{savingTtsSettings ? 'Saving...' : 'Save TTS Settings'}
					</button>
				</div>
			</div>

			<!-- Speech-to-Text Models -->
			<div class="settings-group">
				<h4>Speech-to-Text</h4>
				<p class="group-description">Configure speech recognition settings.</p>

				{#each Object.entries(groupSttModelsByProvider()) as [provider, models]}
					<div class="provider-section">
						<div class="provider-header">{provider}</div>
						<div class="model-cards stt-cards">
							{#each models as model}
								<button
									onclick={() => { selectedSttModel = model.id; saveSttModel(); }}
									disabled={savingSttModel}
									class="model-card {model.id === currentSttModel ? 'active' : ''}"
								>
									<div class="model-card-header">
										<span class="model-card-name">{model.displayName}</span>
									</div>
									<p class="model-card-desc">{model.description}</p>
									<div class="model-card-capabilities">
										{#if model.capabilities.diarization}
											<span class="capability-badge highlight">Diarization</span>
										{/if}
										{#if model.capabilities.translation}
											<span class="capability-badge">Translation</span>
										{/if}
										{#if model.capabilities.timestamps}
											<span class="capability-badge">Timestamps</span>
										{/if}
										{#if model.capabilities.realtime}
											<span class="capability-badge">Realtime</span>
										{/if}
									</div>
									<div class="model-card-footer">
										<span class="model-card-price">${model.pricePerMinute.toFixed(3)}/min</span>
										<span class="model-card-resolution">Max {model.maxFileSizeMB}MB</span>
									</div>
									<div class="model-card-formats">
										{model.inputFormats.slice(0, 5).join(', ')}{model.inputFormats.length > 5 ? '...' : ''}
									</div>
								</button>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		</div>
	{/if}

	<!-- SYSTEM > NOTIFICATIONS -->
	{#if activeSection === 'notifications'}
		<div class="section-content">
			<div class="section-header">
				<h3>Notifications</h3>
				<p>Configure alerts and webhooks.</p>
			</div>

			<div class="settings-group">
				<h4>Browser Notifications</h4>
				{#if !browserNotificationsSupported}
					<p class="text-muted">Not supported in this browser.</p>
				{:else if notificationPermission === 'denied'}
					<p class="text-error">Blocked. Enable in browser settings.</p>
				{:else}
					<div class="toggle-row">
						<div>
							<p class="font-medium">Desktop notifications</p>
							<p class="text-muted text-sm">Get notified when sessions complete</p>
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

			{#if $isAdmin}
				<div class="settings-group">
					<h4>Webhooks</h4>
					<WebhookManager />
				</div>
			{/if}
		</div>
	{/if}

	<!-- SYSTEM > CLEANUP -->
	{#if activeSection === 'cleanup'}
		<div class="section-content">
			<div class="section-header">
				<h3>Background Cleanup</h3>
				<p>Automatic cleanup of sessions and files.</p>
			</div>

			{#if loadingCleanup}
				<div class="loading">Loading...</div>
			{:else if cleanupConfig}
				<!-- Status -->
				<div class="cleanup-status {cleanupStatus?.is_sleeping ? 'sleeping' : 'active'}">
					<span class="status-icon">{cleanupStatus?.is_sleeping ? '😴' : '⚡'}</span>
					<div>
						<p class="font-medium">{cleanupStatus?.is_sleeping ? 'Sleeping' : 'Active'}</p>
						<p class="text-muted text-sm">
							{#if cleanupStatus}Idle for {formatIdleTime(cleanupStatus.idle_seconds)}{/if}
						</p>
					</div>
					<span class="text-muted text-sm">Every {cleanupConfig.cleanup_interval_minutes}m</span>
				</div>

				<!-- Sleep Mode -->
				<div class="settings-group">
					<div class="toggle-row">
						<div>
							<h4>Sleep Mode</h4>
							<p class="text-muted text-sm">Pause cleanup when inactive</p>
						</div>
						<button
							onclick={() => cleanupConfig && (cleanupConfig.sleep_mode_enabled = !cleanupConfig.sleep_mode_enabled)}
							class="toggle {cleanupConfig.sleep_mode_enabled ? 'active' : ''}"
						>
							<span class="toggle-knob"></span>
						</button>
					</div>
					{#if cleanupConfig.sleep_mode_enabled}
						<div class="cleanup-inputs">
							<div>
								<label>Sleep after (min)</label>
								<input type="number" min="1" max="60" bind:value={cleanupConfig.sleep_timeout_minutes} class="input" />
							</div>
							<div>
								<label>Cleanup interval (min)</label>
								<input type="number" min="1" max="60" bind:value={cleanupConfig.cleanup_interval_minutes} class="input" />
							</div>
						</div>
					{/if}
				</div>

				<!-- File Cleanup -->
				<div class="settings-group">
					<h4>File Cleanup</h4>
					<div class="cleanup-items">
						<div class="cleanup-item">
							<span>🖼️ Images</span>
							<div class="cleanup-controls">
								{#if cleanupConfig.cleanup_images_enabled}
									<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_images_max_age_days} class="input small" />
									<span class="text-muted">days</span>
								{/if}
								<button
									onclick={() => cleanupConfig && (cleanupConfig.cleanup_images_enabled = !cleanupConfig.cleanup_images_enabled)}
									class="toggle-sm {cleanupConfig.cleanup_images_enabled ? 'active' : ''}"
								>
									<span class="toggle-knob-sm"></span>
								</button>
							</div>
						</div>
						<div class="cleanup-item">
							<span>🎬 Videos</span>
							<div class="cleanup-controls">
								{#if cleanupConfig.cleanup_videos_enabled}
									<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_videos_max_age_days} class="input small" />
									<span class="text-muted">days</span>
								{/if}
								<button
									onclick={() => cleanupConfig && (cleanupConfig.cleanup_videos_enabled = !cleanupConfig.cleanup_videos_enabled)}
									class="toggle-sm {cleanupConfig.cleanup_videos_enabled ? 'active' : ''}"
								>
									<span class="toggle-knob-sm"></span>
								</button>
							</div>
						</div>
						<div class="cleanup-item">
							<span>📄 Shared Files</span>
							<div class="cleanup-controls">
								{#if cleanupConfig.cleanup_shared_files_enabled}
									<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_shared_files_max_age_days} class="input small" />
									<span class="text-muted">days</span>
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

				<!-- Session Timeouts -->
				<div class="settings-group">
					<h4>Session Timeouts</h4>
					<div class="cleanup-inputs three-col">
						<div>
							<label>SDK Session (min)</label>
							<input type="number" min="5" max="480" bind:value={cleanupConfig.sdk_session_max_age_minutes} class="input" />
						</div>
						<div>
							<label>WebSocket (min)</label>
							<input type="number" min="1" max="60" bind:value={cleanupConfig.websocket_max_age_minutes} class="input" />
						</div>
						<div>
							<label>Sync Logs (hrs)</label>
							<input type="number" min="1" max="168" bind:value={cleanupConfig.sync_log_retention_hours} class="input" />
						</div>
					</div>
				</div>

				{#if cleanupError}
					<div class="error-banner">{cleanupError}</div>
				{/if}
				{#if cleanupSuccess}
					<div class="success-banner">{cleanupSuccess}</div>
				{/if}

				<div class="cleanup-actions">
					<button onclick={saveCleanupConfig} disabled={savingCleanup} class="btn btn-primary flex-1">
						{savingCleanup ? 'Saving...' : 'Save Settings'}
					</button>
					<button onclick={loadCleanupPreview} class="btn btn-secondary">Preview</button>
				</div>
			{/if}
		</div>
	{/if}
{/snippet}

<!-- API User Form Modal -->
{#if showCreateForm}
	<div class="modal-overlay" onclick={resetForm}>
		<div class="modal-content" onclick={(e) => e.stopPropagation()}>
			<div class="modal-header">
				<h3>{editingUser ? 'Edit API User' : 'Create API User'}</h3>
				<button onclick={resetForm} class="icon-btn">✕</button>
			</div>

			<div class="modal-body">
				{#if newlyCreatedKey}
					<div class="key-display success">
						<p class="font-medium">API User created!</p>
						<p class="text-muted text-sm">Copy this key now - it won't be shown again:</p>
						<div class="input-row">
							<code class="flex-1">{newlyCreatedKey}</code>
							<button onclick={() => copyToClipboard(newlyCreatedKey || '')} class="btn btn-primary">Copy</button>
						</div>
					</div>
					<button onclick={resetForm} class="btn btn-primary w-full">Done</button>
				{:else}
					<div class="form-stack">
						<div>
							<label>Name *</label>
							<input bind:value={formData.name} class="input" placeholder="My Application" />
						</div>
						<div>
							<label>Description</label>
							<textarea bind:value={formData.description} class="input" rows="2" placeholder="Optional description"></textarea>
						</div>
						<div class="form-row">
							<div>
								<label>Project</label>
								<select bind:value={formData.project_id} class="input">
									<option value="">Default</option>
									{#each projects as project}
										<option value={project.id}>{project.name}</option>
									{/each}
								</select>
							</div>
							<div>
								<label>Profile</label>
								<select bind:value={formData.profile_id} class="input">
									<option value="">Any</option>
									{#each profiles as profile}
										<option value={profile.id}>{profile.name}</option>
									{/each}
								</select>
							</div>
						</div>
						<div class="form-row">
							<button onclick={resetForm} class="btn btn-secondary flex-1">Cancel</button>
							<button onclick={saveApiUser} class="btn btn-primary flex-1">
								{editingUser ? 'Update' : 'Create'}
							</button>
						</div>
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.settings-card-content {
		display: flex;
		height: 100%;
		overflow: hidden;
	}

	.settings-card-content.mobile {
		flex-direction: column;
	}

	/* Sidebar */
	.settings-sidebar {
		width: 160px;
		flex-shrink: 0;
		border-right: 1px solid hsl(var(--border));
		background: hsl(var(--secondary) / 0.5);
		overflow-y: auto;
		padding: 8px;
	}

	.sidebar-category {
		margin-bottom: 4px;
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 10px;
		border: none;
		background: transparent;
		color: hsl(var(--muted-foreground));
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: 6px;
		cursor: pointer;
		transition: all 0.15s;
		text-align: left;
	}

	.category-header:hover {
		background: hsl(var(--accent));
		color: hsl(var(--foreground));
	}

	.category-header.active {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}

	.sidebar-sections {
		margin-left: 20px;
		padding: 4px 0;
	}

	.section-btn {
		display: block;
		width: 100%;
		padding: 6px 10px;
		border: none;
		background: transparent;
		color: hsl(var(--muted-foreground));
		font-size: 0.75rem;
		border-radius: 4px;
		cursor: pointer;
		text-align: left;
		transition: all 0.15s;
	}

	.section-btn:hover {
		background: hsl(var(--accent));
		color: hsl(var(--foreground));
	}

	.section-btn.active {
		color: hsl(var(--primary));
		font-weight: 500;
	}

	/* Content */
	.settings-content {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
	}

	.section-content {
		max-width: 600px;
	}

	.section-header {
		margin-bottom: 20px;
	}

	.section-header h3 {
		font-size: 1.125rem;
		font-weight: 600;
		color: hsl(var(--foreground));
	}

	.section-header p {
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
		margin-top: 4px;
	}

	.section-header .btn {
		margin-top: 8px;
	}

	.settings-group {
		background: hsl(var(--secondary) / 0.5);
		border: 1px solid hsl(var(--border));
		border-radius: 10px;
		padding: 16px;
		margin-bottom: 16px;
	}

	.settings-group h4 {
		font-size: 0.875rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin-bottom: 12px;
		display: flex;
		align-items: center;
		gap: 8px;
	}

	/* Forms */
	.input {
		width: 100%;
		padding: 8px 12px;
		background: hsl(var(--background));
		border: 1px solid hsl(var(--border));
		border-radius: 6px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
	}

	.input:focus {
		outline: none;
		border-color: hsl(var(--primary));
	}

	.input.small {
		width: 60px;
		text-align: center;
	}

	.input-row {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.form-stack {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.form-stack label {
		font-size: 0.75rem;
		font-weight: 500;
		color: hsl(var(--foreground));
		margin-bottom: 4px;
		display: block;
	}

	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	/* Buttons */
	.btn {
		padding: 8px 16px;
		border: none;
		border-radius: 6px;
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-primary {
		background: hsl(var(--primary));
		color: hsl(var(--primary-foreground));
	}

	.btn-primary:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-secondary {
		background: hsl(var(--secondary));
		color: hsl(var(--foreground));
		border: 1px solid hsl(var(--border));
	}

	.btn-secondary:hover:not(:disabled) {
		background: hsl(var(--accent));
	}

	.btn-sm {
		padding: 6px 12px;
		font-size: 0.75rem;
	}

	.icon-btn {
		padding: 6px;
		background: transparent;
		border: none;
		cursor: pointer;
		border-radius: 4px;
		transition: background 0.15s;
	}

	.icon-btn:hover {
		background: hsl(var(--accent));
	}

	.icon-btn.danger:hover {
		background: hsl(var(--destructive) / 0.2);
	}

	/* Theme selector */
	.theme-options {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 8px;
	}

	.theme-btn {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		padding: 12px;
		background: hsl(var(--background));
		border: 2px solid hsl(var(--border));
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.theme-btn:hover {
		border-color: hsl(var(--primary) / 0.5);
	}

	.theme-btn.active {
		border-color: hsl(var(--primary));
		background: hsl(var(--primary) / 0.1);
	}

	.theme-preview {
		width: 36px;
		height: 36px;
		border-radius: 6px;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.25rem;
	}

	.theme-preview.light { background: white; }
	.theme-preview.dark { background: #1a1a2e; }
	.theme-preview.system { background: linear-gradient(135deg, white 50%, #1a1a2e 50%); }

	/* Toggle */
	.toggle {
		width: 44px;
		height: 24px;
		background: hsl(var(--muted));
		border: none;
		border-radius: 12px;
		cursor: pointer;
		position: relative;
		transition: background 0.2s;
	}

	.toggle.active {
		background: hsl(var(--primary));
	}

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 20px;
		height: 20px;
		background: white;
		border-radius: 50%;
		transition: transform 0.2s;
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}

	.toggle-sm {
		width: 36px;
		height: 20px;
	}

	.toggle-knob-sm {
		width: 16px;
		height: 16px;
	}

	.toggle-sm.active .toggle-knob-sm {
		transform: translateX(16px);
	}

	.toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}

	/* Badges */
	.badge {
		font-size: 0.625rem;
		padding: 2px 8px;
		border-radius: 10px;
		font-weight: 500;
	}

	.badge.success {
		background: hsl(var(--success) / 0.15);
		color: hsl(var(--success));
	}

	.badge.error {
		background: hsl(var(--destructive) / 0.15);
		color: hsl(var(--destructive));
	}

	.badge.muted {
		background: hsl(var(--muted));
		color: hsl(var(--muted-foreground));
	}

	.badge.primary {
		background: hsl(var(--primary) / 0.15);
		color: hsl(var(--primary));
	}

	/* Messages */
	.error-banner {
		background: hsl(var(--destructive) / 0.1);
		border: 1px solid hsl(var(--destructive) / 0.3);
		color: hsl(var(--destructive));
		padding: 10px 14px;
		border-radius: 8px;
		font-size: 0.875rem;
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 16px;
	}

	.success-banner {
		background: hsl(var(--success) / 0.1);
		border: 1px solid hsl(var(--success) / 0.3);
		color: hsl(var(--success));
		padding: 10px 14px;
		border-radius: 8px;
		font-size: 0.875rem;
		margin-bottom: 16px;
	}

	.close-btn {
		background: none;
		border: none;
		font-size: 1.25rem;
		cursor: pointer;
		opacity: 0.7;
	}

	.close-btn:hover {
		opacity: 1;
	}

	/* Utility */
	.text-muted { color: hsl(var(--muted-foreground)); }
	.text-success { color: hsl(var(--success)); }
	.text-error { color: hsl(var(--destructive)); }
	.text-primary { color: hsl(var(--primary)); }
	.text-sm { font-size: 0.875rem; }
	.font-medium { font-weight: 500; }
	.font-mono { font-family: monospace; }
	.flex-1 { flex: 1; }
	.w-full { width: 100%; }

	/* Connection cards */
	.connection-card {
		background: hsl(var(--secondary) / 0.5);
		border: 1px solid hsl(var(--border));
		border-radius: 10px;
		padding: 16px;
		margin-bottom: 12px;
	}

	.connection-header {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 12px;
	}

	.connection-icon {
		width: 40px;
		height: 40px;
		border-radius: 8px;
		background: hsl(var(--muted));
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.25rem;
	}

	.connection-icon.orange { background: hsl(30 80% 50% / 0.15); }

	.connection-title {
		display: flex;
		align-items: center;
		gap: 8px;
		font-weight: 500;
	}

	.connection-actions {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.oauth-flow {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* Docker info */
	.docker-info {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 12px;
		background: hsl(210 80% 50% / 0.1);
		border-radius: 8px;
	}

	.docker-info code {
		background: hsl(var(--muted));
		padding: 2px 6px;
		border-radius: 4px;
		font-size: 0.75rem;
	}

	/* Info card */
	.info-card {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px;
		border-radius: 8px;
	}

	.info-card.success {
		background: hsl(var(--success) / 0.1);
	}

	.info-card .icon {
		font-size: 1.5rem;
	}

	.info-card h4 {
		font-size: 0.875rem;
		font-weight: 500;
		margin: 0;
	}

	.info-card p {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin: 2px 0 0;
	}

	/* User list */
	.user-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.user-card {
		background: hsl(var(--secondary) / 0.5);
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		padding: 12px;
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 12px;
	}

	.user-info {
		flex: 1;
		min-width: 0;
	}

	.user-header {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}

	.user-name {
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.user-desc {
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
		margin: 4px 0;
	}

	.user-meta {
		display: flex;
		gap: 16px;
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	.user-actions {
		display: flex;
		gap: 4px;
	}

	.loading, .empty-state {
		text-align: center;
		padding: 32px;
		color: hsl(var(--muted-foreground));
	}

	/* API Key cards */
	.api-key-card {
		background: hsl(var(--secondary) / 0.5);
		border: 1px solid hsl(var(--border));
		border-radius: 10px;
		padding: 16px;
		margin-bottom: 12px;
	}

	.api-key-header {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 12px;
	}

	.api-icon {
		width: 40px;
		height: 40px;
		border-radius: 8px;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.25rem;
	}

	.api-icon.openai { background: hsl(145 60% 40% / 0.15); }
	.api-icon.google { background: hsl(210 80% 50% / 0.15); }

	.api-title {
		display: flex;
		align-items: center;
		gap: 8px;
		font-weight: 500;
	}

	.api-key-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* Model grid */
	.model-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
		gap: 8px;
	}

	.model-btn {
		display: flex;
		flex-direction: column;
		padding: 10px;
		background: hsl(var(--background));
		border: 2px solid hsl(var(--border));
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.15s;
		text-align: left;
	}

	.model-btn:hover:not(:disabled) {
		border-color: hsl(var(--primary) / 0.5);
	}

	.model-btn.active {
		border-color: hsl(var(--primary));
		background: hsl(var(--primary) / 0.1);
	}

	.model-btn.disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.model-name {
		font-size: 0.75rem;
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.model-price {
		font-size: 0.625rem;
		color: hsl(var(--primary));
		margin-top: auto;
		padding-top: 4px;
	}

	.model-provider {
		font-size: 0.625rem;
		color: hsl(var(--muted-foreground));
	}

	/* Audio models */
	.audio-models {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.audio-model-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.audio-btn {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 8px 12px;
		background: hsl(var(--background));
		border: 2px solid hsl(var(--border));
		border-radius: 6px;
		cursor: pointer;
		font-size: 0.75rem;
		transition: all 0.15s;
	}

	.audio-btn:hover:not(:disabled) {
		border-color: hsl(var(--primary) / 0.5);
	}

	.audio-btn.active {
		border-color: hsl(var(--primary));
		background: hsl(var(--primary) / 0.1);
	}

	.audio-btn .price {
		color: hsl(var(--primary));
	}

	/* Cleanup */
	.cleanup-status {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px 16px;
		border-radius: 8px;
		margin-bottom: 16px;
	}

	.cleanup-status.active {
		background: hsl(var(--success) / 0.1);
	}

	.cleanup-status.sleeping {
		background: hsl(45 80% 50% / 0.1);
	}

	.status-icon {
		font-size: 1.5rem;
	}

	.cleanup-inputs {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
		margin-top: 12px;
	}

	.cleanup-inputs.three-col {
		grid-template-columns: repeat(3, 1fr);
	}

	.cleanup-inputs label {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		display: block;
		margin-bottom: 4px;
	}

	.cleanup-items {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.cleanup-item {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 10px 12px;
		background: hsl(var(--muted) / 0.5);
		border-radius: 6px;
	}

	.cleanup-controls {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.cleanup-actions {
		display: flex;
		gap: 12px;
		margin-top: 16px;
	}

	/* Key display */
	.key-display {
		background: hsl(var(--success) / 0.1);
		border: 1px solid hsl(var(--success) / 0.3);
		border-radius: 8px;
		padding: 12px;
		margin-top: 12px;
	}

	.key-display code {
		background: hsl(var(--background));
		padding: 8px 12px;
		border-radius: 6px;
		font-size: 0.75rem;
		word-break: break-all;
	}

	/* Modal */
	.modal-overlay {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.7);
		backdrop-filter: blur(4px);
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 16px;
		z-index: 100;
	}

	.modal-content {
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 12px;
		width: 100%;
		max-width: 450px;
		max-height: 90vh;
		overflow-y: auto;
	}

	.modal-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 16px;
		border-bottom: 1px solid hsl(var(--border));
	}

	.modal-header h3 {
		font-size: 1rem;
		font-weight: 600;
	}

	.modal-body {
		padding: 16px;
	}

	/* Mobile */
	.mobile-header {
		padding: 12px;
		border-bottom: 1px solid hsl(var(--border));
		background: hsl(var(--secondary) / 0.5);
	}

	.menu-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		background: none;
		border: none;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
	}

	.mobile-menu {
		padding: 8px;
		border-bottom: 1px solid hsl(var(--border));
		background: hsl(var(--secondary) / 0.3);
	}

	.mobile-category {
		margin-bottom: 4px;
	}

	.mobile-category .category-btn {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 10px 12px;
		background: transparent;
		border: none;
		color: hsl(var(--muted-foreground));
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 6px;
		cursor: pointer;
		text-align: left;
	}

	.mobile-category .category-btn.active {
		color: hsl(var(--primary));
		background: hsl(var(--primary) / 0.1);
	}

	.mobile-sections {
		margin-left: 28px;
		padding: 4px 0;
	}

	.mobile-content {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
	}

	/* Models Section */
	.models-section {
		max-width: 800px;
	}

	.group-description {
		font-size: 0.8125rem;
		color: hsl(var(--muted-foreground));
		margin-bottom: 16px;
	}

	.provider-section {
		margin-bottom: 16px;
	}

	.provider-section:last-child {
		margin-bottom: 0;
	}

	.provider-header {
		font-size: 0.75rem;
		font-weight: 600;
		color: hsl(var(--muted-foreground));
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin-bottom: 10px;
		padding-bottom: 6px;
		border-bottom: 1px solid hsl(var(--border) / 0.5);
	}

	.model-cards {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
		gap: 10px;
	}

	.model-cards.tts-cards,
	.model-cards.stt-cards {
		grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
	}

	.model-card {
		display: flex;
		flex-direction: column;
		padding: 12px;
		background: hsl(var(--background));
		border: 2px solid hsl(var(--border));
		border-radius: 10px;
		cursor: pointer;
		transition: all 0.15s;
		text-align: left;
		gap: 6px;
	}

	.model-card:hover:not(:disabled) {
		border-color: hsl(var(--primary) / 0.5);
		background: hsl(var(--primary) / 0.02);
	}

	.model-card.active {
		border-color: hsl(var(--primary));
		background: hsl(var(--primary) / 0.08);
	}

	.model-card.deprecated {
		opacity: 0.7;
	}

	.model-card.deprecated:hover {
		border-color: hsl(45 80% 50% / 0.5);
	}

	.model-card:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.model-card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}

	.model-card-name {
		font-size: 0.875rem;
		font-weight: 600;
		color: hsl(var(--foreground));
	}

	.model-card-desc {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		line-height: 1.4;
		margin: 0;
	}

	.model-card-capabilities {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin-top: 4px;
	}

	.capability-badge {
		font-size: 0.625rem;
		padding: 2px 6px;
		border-radius: 4px;
		background: hsl(var(--muted));
		color: hsl(var(--muted-foreground));
		font-weight: 500;
	}

	.capability-badge.highlight {
		background: hsl(var(--primary) / 0.15);
		color: hsl(var(--primary));
	}

	.capability-badge.audio {
		background: hsl(280 60% 50% / 0.15);
		color: hsl(280 60% 50%);
	}

	.capability-badge.text-excellent {
		background: hsl(var(--success) / 0.15);
		color: hsl(var(--success));
	}

	.capability-badge.text-good {
		background: hsl(210 80% 50% / 0.15);
		color: hsl(210 80% 50%);
	}

	.capability-badge.text-basic {
		background: hsl(45 80% 50% / 0.15);
		color: hsl(45 80% 40%);
	}

	.model-card-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-top: auto;
		padding-top: 8px;
		border-top: 1px solid hsl(var(--border) / 0.5);
	}

	.model-card-price {
		font-size: 0.75rem;
		font-weight: 600;
		color: hsl(var(--primary));
	}

	.model-card-resolution {
		font-size: 0.6875rem;
		color: hsl(var(--muted-foreground));
	}

	.model-card-formats {
		font-size: 0.625rem;
		color: hsl(var(--muted-foreground));
		font-style: italic;
	}

	.badge.warning {
		background: hsl(45 80% 50% / 0.15);
		color: hsl(45 80% 35%);
	}

	/* TTS Settings */
	.tts-settings {
		margin-top: 16px;
		padding: 16px;
		background: hsl(var(--muted) / 0.3);
		border-radius: 8px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.tts-setting-row {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.tts-setting-row label {
		font-size: 0.75rem;
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.slider {
		width: 100%;
		height: 6px;
		-webkit-appearance: none;
		appearance: none;
		background: hsl(var(--muted));
		border-radius: 3px;
		outline: none;
	}

	.slider::-webkit-slider-thumb {
		-webkit-appearance: none;
		appearance: none;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: hsl(var(--primary));
		cursor: pointer;
	}

	.slider::-moz-range-thumb {
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: hsl(var(--primary));
		cursor: pointer;
		border: none;
	}

	@media (max-width: 640px) {
		.audio-models {
			grid-template-columns: 1fr;
		}

		.cleanup-inputs.three-col {
			grid-template-columns: 1fr;
		}

		.form-row {
			grid-template-columns: 1fr;
		}

		.model-cards {
			grid-template-columns: 1fr;
		}

		.model-cards.tts-cards,
		.model-cards.stt-cards {
			grid-template-columns: 1fr;
		}
	}
</style>
