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
	import { slide, fade, fly } from 'svelte/transition';
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

	// Navigation state
	type Category = 'account' | 'api-users' | 'ai-models' | 'system';
	type Section =
		| 'general' | 'security' | 'authentication'
		| 'users' | 'rate-limits' | 'credential-policies'
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

	// Form state for API Users - inline form mode
	let formMode: 'closed' | 'create' | 'edit' = $state('closed');
	let editingUser: ApiUser | null = $state(null);
	let newlyCreatedKey: string | null = $state(null);
	let regeneratedKey: string | null = $state(null);

	// Per-user credential policies state (shown within each user card)
	let userPolicies: Map<string, UserCredentialPolicy[]> = $state(new Map());
	let loadingUserPolicies = $state<string | null>(null);
	let savingUserPolicy = $state<string | null>(null); // format: "userId:credentialType"
	let expandedUserPolicies = $state<string | null>(null); // userId of expanded policies section

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

	// Meshy settings
	let meshyApiKey = $state('');
	let meshyApiKeyMasked = $state('');
	let savingMeshyKey = $state(false);
	let meshyKeySuccess = $state('');
	let meshyKeyError = $state('');

	// DeepSeek settings
	let deepseekApiKey = $state('');
	let deepseekApiKeyMasked = $state('');
	let savingDeepseekKey = $state(false);
	let deepseekKeySuccess = $state('');
	let deepseekKeyError = $state('');

	// 3D Model settings
	let selected3DModel = $state('meshy-6');
	let current3DModel = $state('meshy-6');
	let saving3DModel = $state(false);
	let model3DSuccess = $state('');
	let model3DError = $state('');

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
	interface CleanupPreview {
		images: { name: string; size: number; project: string; age_days: number }[];
		videos: { name: string; size: number; project: string; age_days: number }[];
		shared_files: { name: string; size: number; project: string; age_days: number }[];
		uploads: { name: string; size: number; project: string; age_days: number }[];
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

	// Per-user credential policy functions
	async function loadUserPolicies(userId: string) {
		loadingUserPolicies = userId;
		try {
			const response = await getUserCredentialPolicies(userId);
			userPolicies.set(userId, response.policies);
			userPolicies = new Map(userPolicies); // Trigger reactivity
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
			// Load policies if not already loaded
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
				meshy_api_key_set: boolean;
				meshy_api_key_masked: string;
				model_3d_model: string | null;
				deepseek_api_key_set: boolean;
				deepseek_api_key_masked: string;
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
			// Meshy settings
			meshyApiKeyMasked = settings.meshy_api_key_masked || '';
			selected3DModel = settings.model_3d_model || 'meshy-6';
			current3DModel = selected3DModel;
			// DeepSeek settings
			deepseekApiKeyMasked = settings.deepseek_api_key_masked || '';
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

	// Meshy API Key function
	async function saveMeshyKey() {
		if (!meshyApiKey.trim()) return;
		savingMeshyKey = true;
		meshyKeyError = '';
		meshyKeySuccess = '';
		try {
			await api.post('/meshy/config/api-key', {
				api_key: meshyApiKey
			});
			meshyKeySuccess = 'Meshy API key saved';
			meshyApiKey = '';
			await loadIntegrationSettings();
			setTimeout(() => meshyKeySuccess = '', 3000);
		} catch (e: any) {
			meshyKeyError = e.detail || 'Failed to save Meshy key';
		}
		savingMeshyKey = false;
	}

	// DeepSeek API Key function
	async function saveDeepseekKey() {
		if (!deepseekApiKey.trim()) return;
		savingDeepseekKey = true;
		deepseekKeyError = '';
		deepseekKeySuccess = '';
		try {
			await api.post('/settings/integrations/deepseek', {
				api_key: deepseekApiKey
			});
			deepseekKeySuccess = 'DeepSeek API key saved';
			deepseekApiKey = '';
			await loadIntegrationSettings();
			setTimeout(() => deepseekKeySuccess = '', 3000);
		} catch (e: any) {
			deepseekKeyError = e.detail || 'Failed to save DeepSeek key';
		}
		savingDeepseekKey = false;
	}

	// 3D Model function
	async function save3DModel() {
		if (selected3DModel === current3DModel) return;
		saving3DModel = true;
		model3DError = '';
		model3DSuccess = '';
		try {
			await api.put('/meshy/config/settings', {
				model: selected3DModel
			});
			current3DModel = selected3DModel;
			model3DSuccess = '3D model saved';
			setTimeout(() => model3DSuccess = '', 3000);
		} catch (e: any) {
			model3DError = e.detail || 'Failed to save 3D model';
		}
		saving3DModel = false;
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
			// Save current config first so preview reflects UI state
			if (cleanupConfig) {
				await api.post('/settings/cleanup', cleanupConfig);
			}
			cleanupPreview = await api.get<CleanupPreview>('/settings/cleanup/preview');
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
				uploads_deleted: number;
				bytes_freed: number;
				bytes_freed_formatted: string;
			}>('/settings/cleanup/run');
			const total = result.images_deleted + result.videos_deleted + result.shared_files_deleted + result.uploads_deleted;
			if (total > 0) {
				cleanupSuccess = `Cleanup complete: ${total} files deleted (${result.bytes_freed_formatted} freed)`;
			} else {
				cleanupSuccess = 'Cleanup complete: No files to delete';
			}
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

	// API User functions - inline form mode
	function openCreateForm() {
		editingUser = null;
		formData = { name: '', description: '', project_id: '', profile_ids: [], web_login_allowed: true };
		newlyCreatedKey = null;
		formMode = 'create';
	}

	function openEditForm(user: ApiUser) {
		editingUser = user;
		formData = {
			name: user.name,
			description: user.description || '',
			project_id: user.project_id || '',
			profile_ids: user.profile_ids || [],
			web_login_allowed: user.web_login_allowed
		};
		formMode = 'edit';
	}

	function closeForm() {
		formMode = 'closed';
		editingUser = null;
		newlyCreatedKey = null;
		regeneratedKey = null;
		formData = { name: '', description: '', project_id: '', profile_ids: [], web_login_allowed: true };
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
				closeForm();
			} else {
				const result = await api.post<ApiUserWithKey>('/api-users', formData);
				newlyCreatedKey = result.api_key;
				await loadData();
				// Stay in form mode to show the key
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
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="settings-card-content">
			<div class="settings-inner">
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

	<!-- API & USERS > USERS (with inline Key Policies) -->
	{#if activeSection === 'users'}
		<div class="section-content api-users-section">
			<!-- Section Header -->
			<div class="section-header">
				<div class="section-title-row">
					<h3>API Users</h3>
					<p>Manage programmatic access and credential policies.</p>
				</div>
				{#if formMode === 'closed'}
					<button onclick={openCreateForm} class="btn btn-primary btn-glow">
						<span class="btn-icon">+</span>
						Create User
					</button>
				{/if}
			</div>

			<!-- Scrollable Content Area -->
			<div class="api-users-scroll-area">
				<!-- Inline Form Panel -->
				{#if formMode !== 'closed'}
					<div class="inline-form-panel" transition:slide={{ duration: 300, easing: quintOut }}>
						<div class="form-panel-header">
							<h4>{formMode === 'edit' ? 'Edit API User' : 'Create API User'}</h4>
							<button onclick={closeForm} class="icon-btn close-form" title="Close">✕</button>
						</div>

						{#if newlyCreatedKey}
							<div class="key-success-panel" transition:fade={{ duration: 200 }}>
								<div class="success-icon">🔑</div>
								<h5>API User Created Successfully</h5>
								<p class="key-warning">Copy this key now — it won't be shown again:</p>
								<div class="key-display-inline">
									<code>{newlyCreatedKey}</code>
									<button
										onclick={() => { copyToClipboard(newlyCreatedKey || ''); closeForm(); }}
										class="btn btn-primary btn-glow"
									>
										Copy & Close
									</button>
								</div>
							</div>
						{:else}
							<div class="form-grid">
								<div class="form-field full-width">
									<label>Name <span class="required">*</span></label>
									<input
										bind:value={formData.name}
										class="input input-lg"
										placeholder="My Application"
									/>
								</div>
								<div class="form-field full-width">
									<label>Description</label>
									<textarea
										bind:value={formData.description}
										class="input"
										rows="2"
										placeholder="Optional description"
									></textarea>
								</div>
								<div class="form-field">
									<label>Project</label>
									<select bind:value={formData.project_id} class="input">
										<option value="">Default</option>
										{#each projects as project}
											<option value={project.id}>{project.name}</option>
										{/each}
									</select>
								</div>
								<div class="form-field full-width">
									<label>Allowed Profiles</label>
									<p class="field-hint" style="margin-top: 0; margin-bottom: var(--space-2);">
										{formData.profile_ids.length === 0 ? 'No restriction - user can access any profile' : `${formData.profile_ids.length} profile${formData.profile_ids.length !== 1 ? 's' : ''} selected`}
									</p>
									<div class="profile-multiselect">
										{#each profiles as profile}
											<label class="profile-checkbox-item">
												<input
													type="checkbox"
													checked={formData.profile_ids.includes(profile.id)}
													onchange={(e) => {
														const target = e.target as HTMLInputElement;
														if (target.checked) {
															formData.profile_ids = [...formData.profile_ids, profile.id];
														} else {
															formData.profile_ids = formData.profile_ids.filter(id => id !== profile.id);
														}
													}}
												/>
												<span class="profile-name">{profile.name}</span>
											</label>
										{/each}
										{#if profiles.length === 0}
											<p class="empty-profiles-hint">No profiles available</p>
										{/if}
									</div>
								</div>
								<div class="form-field full-width">
									<label class="checkbox-label">
										<input type="checkbox" bind:checked={formData.web_login_allowed} />
										<span>Allow web login</span>
									</label>
									<p class="field-hint">User can register with the API key and set credentials for web access</p>
								</div>
								{#if editingUser?.username}
									<div class="info-box full-width">
										<p><strong>Registered as:</strong> @{editingUser.username}</p>
									</div>
								{/if}
							</div>
							<div class="form-actions">
								<button onclick={closeForm} class="btn btn-secondary">Cancel</button>
								<button onclick={saveApiUser} class="btn btn-primary btn-glow">
									{formMode === 'edit' ? 'Update User' : 'Create User'}
								</button>
							</div>
						{/if}
					</div>
				{/if}

				<!-- User List -->
				{#if loading}
					<div class="loading-state">
						<div class="loading-spinner"></div>
						<span>Loading users...</span>
					</div>
				{:else if apiUsers.length === 0}
					<div class="empty-state atmospheric">
						<div class="empty-icon">👤</div>
						<p class="empty-title">No API users yet</p>
						<p class="empty-desc">Create your first user to enable external access.</p>
					</div>
				{:else}
					<div class="user-list">
						{#each apiUsers as user, index}
							<div
								class="user-card"
								class:active={user.is_active}
								class:inactive={!user.is_active}
								class:editing={editingUser?.id === user.id}
								class:policies-expanded={expandedUserPolicies === user.id}
								style="animation-delay: {index * 60}ms"
							>
								<div class="user-card-main">
									<div class="user-info">
										<div class="user-header">
											<span class="user-name">{user.name}</span>
											{#if user.username}
												<span class="badge primary">@{user.username}</span>
											{/if}
											<span class="badge {user.is_active ? 'success' : 'error'}">{user.is_active ? 'Active' : 'Inactive'}</span>
											{#if user.web_login_allowed}
												<span class="badge info" title="Can login via web with username/password">🌐 Web</span>
											{/if}
										</div>
										{#if user.description}
											<p class="user-desc">{user.description}</p>
										{/if}
										<div class="user-meta">
											<span>Profiles: {user.profile_ids?.length ? user.profile_ids.map(id => profiles.find(p => p.id === id)?.name || id).join(', ') : 'Any'}</span>
											{#if user.username}
												<span>Web Login: ✓ Registered</span>
											{:else if user.web_login_allowed}
												<span>Web Login: Pending registration</span>
											{:else}
												<span>Web Login: Disabled</span>
											{/if}
											<span>Last used: {formatDate(user.last_used_at)}</span>
										</div>
									</div>
									<div class="user-actions">
										<button onclick={() => toggleUserPolicies(user.id)} class="icon-btn" title="Key Policies" class:active={expandedUserPolicies === user.id}>🔑</button>
										<button onclick={() => openEditForm(user)} class="icon-btn" title="Edit">✏️</button>
										<button onclick={() => regenerateKey(user.id)} class="icon-btn" title="Regenerate Key">🔄</button>
										<button onclick={() => toggleActive(user)} class="icon-btn" title={user.is_active ? 'Deactivate' : 'Activate'}>
											{user.is_active ? '⏸️' : '▶️'}
										</button>
										<button onclick={() => deleteUser(user.id)} class="icon-btn danger" title="Delete">🗑️</button>
									</div>
								</div>
							</div>

							<!-- Per-user Key Policies (below card) -->
							{#if expandedUserPolicies === user.id}
								<div class="user-policies-below" transition:slide={{ duration: 200, easing: quintOut }}>
									<div class="policies-below-header">
										<span class="policies-below-title">🔑 Key Policies</span>
										<span class="policies-below-hint">Configure API key access for {user.name}</span>
									</div>
									{#if loadingUserPolicies === user.id}
										<div class="policies-loading">
											<div class="loading-spinner small"></div>
											<span>Loading policies...</span>
										</div>
									{:else if userPolicies.has(user.id)}
										<div class="policies-below-list">
											{#each userPolicies.get(user.id) || [] as policy}
												<div class="policy-row" class:has-override={policy.source === 'user'}>
													<div class="policy-row-info">
														<span class="policy-row-name">{policy.name}</span>
														{#if policy.source === 'user'}
															<span class="policy-custom-badge">Custom</span>
														{/if}
													</div>
													<div class="policy-row-control">
														<select
															value={policy.policy}
															onchange={(e) => handleUserPolicyChange(user.id, policy.credential_type, (e.currentTarget as HTMLSelectElement).value as CredentialPolicyType)}
															disabled={savingUserPolicy === `${user.id}:${policy.credential_type}`}
															class="policy-row-select"
														>
															<option value="admin_provided">Admin Provides</option>
															<option value="user_provided">User Must Provide</option>
															<option value="optional">Optional</option>
														</select>
														{#if savingUserPolicy === `${user.id}:${policy.credential_type}`}
															<div class="saving-indicator"></div>
														{/if}
													</div>
												</div>
											{/each}
										</div>
									{:else}
										<div class="policies-empty">No credential policies available</div>
									{/if}
								</div>
							{/if}
						{/each}
					</div>
				{/if}

				<!-- Regenerated Key Display (floating) -->
				{#if regeneratedKey}
					<div class="key-display floating" transition:fly={{ y: 20, duration: 300 }}>
						<p class="font-medium">New API Key Generated</p>
						<div class="input-row">
							<code class="flex-1">{regeneratedKey}</code>
							<button onclick={() => { copyToClipboard(regeneratedKey || ''); regeneratedKey = null; }} class="btn btn-primary">Copy</button>
						</div>
					</div>
				{/if}
			</div>
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

			<!-- Meshy AI -->
			<div class="api-key-card">
				<div class="api-key-header">
					<div class="api-icon meshy">
						<svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
							<polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
							<line x1="12" y1="22.08" x2="12" y2="12"/>
						</svg>
					</div>
					<div>
						<div class="api-title">
							<span>Meshy AI</span>
							{#if meshyApiKeyMasked}
								<span class="badge success">Configured</span>
							{/if}
						</div>
						<p class="text-muted text-sm">3D model generation</p>
					</div>
				</div>
				<div class="api-key-form">
					{#if meshyApiKeyMasked}
						<p class="text-muted text-sm font-mono">{meshyApiKeyMasked}</p>
					{/if}
					<div class="input-row">
						<input type="password" bind:value={meshyApiKey} placeholder={meshyApiKeyMasked ? 'Enter new key...' : 'msy_...'} class="input flex-1 font-mono" />
						<button onclick={saveMeshyKey} disabled={savingMeshyKey || !meshyApiKey.trim()} class="btn btn-primary">
							{savingMeshyKey ? '...' : 'Save'}
						</button>
					</div>
					{#if meshyKeySuccess}
						<p class="text-success text-sm">{meshyKeySuccess}</p>
					{:else if meshyKeyError}
						<p class="text-error text-sm">{meshyKeyError}</p>
					{/if}
				</div>
			</div>

			<!-- DeepSeek -->
			<div class="api-key-card">
				<div class="api-key-header">
					<div class="api-icon deepseek">🔮</div>
					<div>
						<div class="api-title">
							<span>DeepSeek</span>
							{#if deepseekApiKeyMasked}
								<span class="badge success">Configured</span>
							{/if}
						</div>
						<p class="text-muted text-sm">AI reasoning & collaboration</p>
					</div>
				</div>
				<div class="api-key-form">
					{#if deepseekApiKeyMasked}
						<p class="text-muted text-sm font-mono">{deepseekApiKeyMasked}</p>
					{/if}
					<div class="input-row">
						<input type="password" bind:value={deepseekApiKey} placeholder={deepseekApiKeyMasked ? 'Enter new key...' : 'sk-...'} class="input flex-1 font-mono" />
						<button onclick={saveDeepseekKey} disabled={savingDeepseekKey || !deepseekApiKey.trim()} class="btn btn-primary">
							{savingDeepseekKey ? '...' : 'Save'}
						</button>
					</div>
					{#if deepseekKeySuccess}
						<p class="text-success text-sm">{deepseekKeySuccess}</p>
					{:else if deepseekKeyError}
						<p class="text-error text-sm">{deepseekKeyError}</p>
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

			<!-- 3D Generation Models -->
			<div class="settings-group">
				<h4>3D Generation</h4>
				<p class="group-description">Select the AI model for 3D generation.</p>

				<div class="provider-section">
					<div class="provider-header">Meshy AI</div>
					<div class="input-group">
						<select
							class="input"
							bind:value={selected3DModel}
							onchange={save3DModel}
							disabled={saving3DModel}
						>
							<option value="meshy-6">Meshy 6 (Latest) - Best Quality</option>
							<option value="meshy-5">Meshy 5 - Balanced</option>
							<option value="meshy-4">Meshy 4 - Fast</option>
						</select>
					</div>
					{#if model3DSuccess}
						<p class="text-success text-sm mt-2">{model3DSuccess}</p>
					{/if}
					{#if model3DError}
						<p class="text-error text-sm mt-2">{model3DError}</p>
					{/if}
				</div>
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
							<div class="cleanup-item-label">
								<span>🖼️ Images</span>
								{#if cleanupPreview && cleanupPreview.images.length > 0}
									<span class="preview-count">{cleanupPreview.images.length} files</span>
								{/if}
							</div>
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
							<div class="cleanup-item-label">
								<span>🎬 Videos</span>
								{#if cleanupPreview && cleanupPreview.videos.length > 0}
									<span class="preview-count">{cleanupPreview.videos.length} files</span>
								{/if}
							</div>
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
							<div class="cleanup-item-label">
								<span>📄 Shared Files</span>
								{#if cleanupPreview && cleanupPreview.shared_files.length > 0}
									<span class="preview-count">{cleanupPreview.shared_files.length} files</span>
								{/if}
							</div>
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
						<div class="cleanup-item">
							<div class="cleanup-item-label">
								<span>📁 Uploads</span>
								{#if cleanupPreview && cleanupPreview.uploads.length > 0}
									<span class="preview-count">{cleanupPreview.uploads.length} files</span>
								{/if}
							</div>
							<div class="cleanup-controls">
								{#if cleanupConfig.cleanup_uploads_enabled}
									<input type="number" min="1" max="365" bind:value={cleanupConfig.cleanup_uploads_max_age_days} class="input small" />
									<span class="text-muted">days</span>
								{/if}
								<button
									onclick={() => cleanupConfig && (cleanupConfig.cleanup_uploads_enabled = !cleanupConfig.cleanup_uploads_enabled)}
									class="toggle-sm {cleanupConfig.cleanup_uploads_enabled ? 'active' : ''}"
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

				<!-- Preview Summary (inline) -->
				{#if cleanupPreview}
					<div class="cleanup-preview-inline {cleanupPreview.total_count > 0 ? 'has-files' : 'no-files'}">
						{#if cleanupPreview.total_count === 0}
							<span class="preview-status">✓ No files match cleanup criteria</span>
						{:else}
							<span class="preview-status">⚠ {cleanupPreview.total_count} files ({cleanupPreview.total_bytes_formatted}) would be deleted</span>
						{/if}
						<button onclick={() => { cleanupPreview = null; }} class="preview-clear" title="Clear preview">✕</button>
					</div>
				{/if}

				<div class="cleanup-actions">
					<button onclick={saveCleanupConfig} disabled={savingCleanup} class="btn btn-primary flex-1">
						{savingCleanup ? 'Saving...' : 'Save Settings'}
					</button>
					<button onclick={loadCleanupPreview} class="btn btn-secondary">Preview</button>
					{#if cleanupPreview && cleanupPreview.total_count > 0}
						<button onclick={runCleanupNow} disabled={runningCleanup} class="btn btn-danger">
							{runningCleanup ? 'Deleting...' : 'Delete Files'}
						</button>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
{/snippet}

<style>
	/* ═══════════════════════════════════════════════════════════════════════════════
	   NEO-GLASS DESIGN SYSTEM - Settings Card
	   ═══════════════════════════════════════════════════════════════════════════════

	   A refined glassmorphism aesthetic with:
	   - Translucent layered surfaces
	   - Subtle depth through shadows
	   - OKLch color space for perceptual uniformity
	   - Micro-interactions with spring transitions
	   - 8px grid spacing system

	   ═══════════════════════════════════════════════════════════════════════════════ */

	/* ═══════════════════════════════════════════════════════════════════════════════
	   ROOT CONTAINER & DESIGN TOKENS
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.settings-card-content {
		/* Typography */
		--font-display: 'Inter', system-ui, -apple-system, sans-serif;
		--font-mono: 'JetBrains Mono', 'SF Mono', 'Monaco', monospace;

		/* Type scale */
		--text-xs: 0.6875rem;   /* 11px */
		--text-sm: 0.75rem;     /* 12px */
		--text-base: 0.8125rem; /* 13px */
		--text-md: 0.875rem;    /* 14px */
		--text-lg: 1rem;        /* 16px */
		--text-xl: 1.125rem;    /* 18px */

		/* Spacing (8px grid) */
		--space-1: 4px;
		--space-2: 8px;
		--space-3: 12px;
		--space-4: 16px;
		--space-5: 20px;
		--space-6: 24px;
		--space-8: 32px;

		/* Border radius */
		--radius-sm: 6px;
		--radius-md: 10px;
		--radius-lg: 14px;
		--radius-xl: 18px;
		--radius-full: 9999px;

		/* Max width for centered layout */
		--settings-max-width: 1000px;

		/* Surface colors */
		--surface-0: var(--card, oklch(0.18 0.008 260));
		--surface-1: color-mix(in oklch, var(--muted, oklch(0.22 0.01 260)) 60%, transparent);
		--surface-2: color-mix(in oklch, var(--muted, oklch(0.22 0.01 260)) 80%, transparent);
		--surface-hover: color-mix(in oklch, var(--primary, oklch(0.72 0.14 180)) 8%, var(--muted, oklch(0.22 0.01 260)));
		--surface-active: color-mix(in oklch, var(--primary, oklch(0.72 0.14 180)) 15%, var(--muted, oklch(0.22 0.01 260)));

		/* Borders */
		--border-subtle: color-mix(in oklch, var(--border, oklch(0.28 0.01 260)) 60%, transparent);
		--border-default: var(--border, oklch(0.28 0.01 260));
		--border-emphasis: color-mix(in oklch, var(--primary, oklch(0.72 0.14 180)) 40%, var(--border, oklch(0.28 0.01 260)));

		/* Text hierarchy */
		--text-primary: var(--foreground, oklch(0.95 0.01 260));
		--text-secondary: var(--muted-foreground, oklch(0.65 0.02 260));
		--text-tertiary: color-mix(in oklch, var(--muted-foreground, oklch(0.65 0.02 260)) 70%, transparent);

		/* Semantic colors */
		--accent-primary: var(--primary, oklch(0.72 0.14 180));
		--accent-success: var(--success, oklch(0.65 0.18 145));
		--accent-warning: var(--warning, oklch(0.75 0.15 85));
		--accent-danger: var(--destructive, oklch(0.55 0.22 25));
		--accent-info: var(--info, oklch(0.65 0.15 240));

		/* Shadows */
		--shadow-inset: inset 0 1px 0 0 rgba(255,255,255,0.03);
		--shadow-xs: 0 1px 2px rgba(0,0,0,0.15);
		--shadow-sm: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.08);
		--shadow-md: 0 4px 6px -1px rgba(0,0,0,0.15), 0 2px 4px -1px rgba(0,0,0,0.08);
		--shadow-lg: 0 10px 15px -3px rgba(0,0,0,0.15), 0 4px 6px -2px rgba(0,0,0,0.08);
		--shadow-glow: 0 0 20px color-mix(in oklch, var(--primary, oklch(0.72 0.14 180)) 30%, transparent);
		--shadow-glow-soft: 0 0 12px color-mix(in oklch, var(--primary, oklch(0.72 0.14 180)) 15%, transparent);

		/* Transitions */
		--transition-fast: 100ms cubic-bezier(0.4, 0, 0.2, 1);
		--transition-base: 150ms cubic-bezier(0.4, 0, 0.2, 1);
		--transition-smooth: 200ms cubic-bezier(0.16, 1, 0.3, 1);
		--transition-spring: 300ms cubic-bezier(0.34, 1.56, 0.64, 1);

		display: flex;
		justify-content: center;
		height: 100%;
		overflow: hidden;
		background: var(--surface-0);
		position: relative;
		font-family: var(--font-display);
	}

	/* Inner container - holds sidebar + content, centered and clamped */
	.settings-inner {
		display: flex;
		width: 100%;
		max-width: var(--settings-max-width);
		height: 100%;
		gap: var(--space-4);
		padding: var(--space-4);
	}

	/* Glass edge highlight at top */
	.settings-card-content::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent 0%,
			color-mix(in oklch, var(--accent-primary) 30%, transparent) 20%,
			color-mix(in oklch, var(--accent-primary) 50%, transparent) 50%,
			color-mix(in oklch, var(--accent-primary) 30%, transparent) 80%,
			transparent 100%
		);
		z-index: 10;
	}

	/* Light mode adjustments */
	:global(.light) .settings-card-content {
		--shadow-xs: 0 1px 2px rgba(0,0,0,0.06);
		--shadow-sm: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
		--shadow-md: 0 4px 6px -1px rgba(0,0,0,0.08), 0 2px 4px -1px rgba(0,0,0,0.04);
		--shadow-lg: 0 10px 15px -3px rgba(0,0,0,0.08), 0 4px 6px -2px rgba(0,0,0,0.04);
	}

	.settings-card-content.mobile {
		flex-direction: column;
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   SIDEBAR - Floating Pill Design
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.settings-sidebar {
		width: 180px;
		flex-shrink: 0;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		overflow-y: auto;
		overflow-x: hidden;
		padding: var(--space-3);
		position: relative;
		box-shadow: var(--shadow-sm);
		/* Frosted glass effect */
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
	}

	/* Subtle inner glow at top */
	.settings-sidebar::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent 0%,
			color-mix(in oklch, var(--foreground) 10%, transparent) 30%,
			color-mix(in oklch, var(--foreground) 15%, transparent) 50%,
			color-mix(in oklch, var(--foreground) 10%, transparent) 70%,
			transparent 100%
		);
		border-radius: var(--radius-lg) var(--radius-lg) 0 0;
	}

	.sidebar-category {
		margin-bottom: var(--space-1);
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		padding: var(--space-2) var(--space-3);
		border: 1px solid transparent;
		background: transparent;
		color: var(--text-secondary);
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: 500;
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: all var(--transition-smooth);
		text-align: left;
	}

	.category-header:hover {
		background: var(--surface-1);
		color: var(--text-primary);
		border-color: var(--border-subtle);
	}

	.category-header.active {
		background: color-mix(in oklch, var(--accent-primary) 12%, transparent);
		color: var(--accent-primary);
		border-color: color-mix(in oklch, var(--accent-primary) 25%, transparent);
		box-shadow: var(--shadow-xs), var(--shadow-glow-soft);
	}

	.sidebar-sections {
		margin-left: var(--space-5);
		padding-left: var(--space-3);
		border-left: 1px solid var(--border-subtle);
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		margin-top: var(--space-1);
		margin-bottom: var(--space-2);
	}

	.section-btn {
		padding: var(--space-2) var(--space-3);
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		color: var(--text-tertiary);
		font-family: var(--font-display);
		font-size: var(--text-sm);
		font-weight: 500;
		cursor: pointer;
		transition: all var(--transition-base);
		text-align: left;
		position: relative;
	}

	.section-btn:hover {
		color: var(--text-primary);
		background: var(--surface-1);
	}

	.section-btn.active {
		color: var(--accent-primary);
		font-weight: 600;
	}

	.section-btn.active::before {
		content: '';
		position: absolute;
		left: calc(-1 * var(--space-3) - 1px);
		top: 50%;
		transform: translateY(-50%);
		width: 3px;
		height: 16px;
		background: var(--accent-primary);
		border-radius: var(--radius-full);
		box-shadow: 0 0 8px var(--accent-primary);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   CONTENT AREA - Floating Panel Design
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.settings-content {
		flex: 1;
		overflow-y: auto;
		padding: var(--space-5) var(--space-6);
		scrollbar-width: thin;
		scrollbar-color: var(--border-default) transparent;
		/* Match floating sidebar style */
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-sm);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		position: relative;
	}

	/* Subtle inner glow at top */
	.settings-content::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent 0%,
			color-mix(in oklch, var(--foreground) 10%, transparent) 30%,
			color-mix(in oklch, var(--foreground) 15%, transparent) 50%,
			color-mix(in oklch, var(--foreground) 10%, transparent) 70%,
			transparent 100%
		);
		border-radius: var(--radius-lg) var(--radius-lg) 0 0;
		z-index: 1;
	}

	.settings-content::-webkit-scrollbar {
		width: 6px;
	}

	.settings-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.settings-content::-webkit-scrollbar-thumb {
		background: var(--border-default);
		border-radius: 3px;
	}

	.settings-content::-webkit-scrollbar-thumb:hover {
		background: var(--text-tertiary);
	}

	.section-content {
		width: 100%;
		max-width: 720px;
	}

	/* Section header */
	.section-header {
		margin-bottom: var(--space-6);
		padding-bottom: var(--space-4);
		border-bottom: 1px solid var(--border-subtle);
		position: relative;
	}

	.section-header::after {
		content: '';
		position: absolute;
		bottom: -1px;
		left: 0;
		width: 60px;
		height: 2px;
		background: linear-gradient(
			90deg,
			var(--accent-primary) 0%,
			transparent 100%
		);
	}

	.section-header h3 {
		font-family: var(--font-display);
		font-size: var(--text-xl);
		font-weight: 600;
		color: var(--text-primary);
		letter-spacing: -0.02em;
		margin: 0;
	}

	.section-header p {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		margin-top: var(--space-2);
		line-height: 1.5;
	}

	.section-header .btn {
		margin-top: var(--space-3);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   SETTINGS GROUPS
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.settings-group {
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		padding: var(--space-5);
		margin-bottom: var(--space-5);
		box-shadow: var(--shadow-inset), var(--shadow-xs);
		transition: all var(--transition-smooth);
		position: relative;
	}

	/* Subtle corner gradient */
	.settings-group::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		width: 60px;
		height: 60px;
		background: linear-gradient(
			135deg,
			color-mix(in oklch, var(--accent-primary) 8%, transparent) 0%,
			transparent 70%
		);
		border-radius: var(--radius-lg) 0 0 0;
		pointer-events: none;
	}

	.settings-group:hover {
		border-color: var(--border-default);
		box-shadow: var(--shadow-inset), var(--shadow-sm);
	}

	.settings-group h4 {
		font-family: var(--font-display);
		font-size: var(--text-md);
		font-weight: 600;
		color: var(--text-primary);
		margin-bottom: var(--space-4);
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}

	.settings-group h4 svg {
		width: 16px;
		height: 16px;
		color: var(--accent-primary);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   FORM ELEMENTS
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.input {
		width: 100%;
		padding: var(--space-3) var(--space-4);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		color: var(--text-primary);
		font-family: var(--font-mono);
		font-size: var(--text-base);
		outline: none;
		transition: all var(--transition-smooth);
		box-shadow: var(--shadow-inset);
	}

	.input::placeholder {
		color: var(--text-tertiary);
		font-family: var(--font-display);
	}

	.input:hover {
		border-color: var(--border-default);
		background: var(--surface-2);
	}

	.input:focus {
		border-color: var(--accent-primary);
		background: var(--surface-0);
		box-shadow: var(--shadow-inset), 0 0 0 3px color-mix(in oklch, var(--accent-primary) 15%, transparent);
	}

	.input.small {
		width: 70px;
		text-align: center;
		padding: var(--space-2) var(--space-2);
	}

	/* Select/dropdown styling */
	select.input {
		cursor: pointer;
		appearance: none;
		-webkit-appearance: none;
		-moz-appearance: none;
		background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%236b7a99' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.5' d='M6 8l4 4 4-4'/%3e%3c/svg%3e");
		background-position: right 0.75rem center;
		background-repeat: no-repeat;
		background-size: 1.25em 1.25em;
		padding-right: 2.75rem;
	}

	select.input option {
		background-color: var(--surface-0);
		color: var(--text-primary);
		padding: var(--space-2);
	}

	.input-row {
		display: flex;
		gap: var(--space-3);
		align-items: center;
	}

	.form-stack {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.form-stack label {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-secondary);
		margin-bottom: var(--space-2);
		display: block;
	}

	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-4);
	}

	.checkbox-row {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.checkbox-label {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		cursor: pointer;
		font-family: var(--font-display);
		color: var(--text-secondary);
		font-size: var(--text-md);
	}

	.checkbox-label input[type="checkbox"] {
		width: 18px;
		height: 18px;
		cursor: pointer;
		accent-color: var(--accent-primary);
	}

	.info-box {
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-left: 3px solid var(--accent-primary);
		border-radius: var(--radius-sm);
		padding: var(--space-3) var(--space-4);
	}

	.badge.info {
		background: color-mix(in oklch, var(--accent-info) 12%, transparent);
		color: var(--accent-info);
		border: 1px solid color-mix(in oklch, var(--accent-info) 25%, transparent);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   BUTTONS
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: var(--space-2);
		padding: var(--space-3) var(--space-4);
		border-radius: var(--radius-md);
		font-family: var(--font-display);
		font-size: var(--text-md);
		font-weight: 500;
		cursor: pointer;
		transition: all var(--transition-smooth);
		border: none;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn:focus-visible {
		outline: 2px solid var(--accent-primary);
		outline-offset: 2px;
	}

	.btn-primary {
		background: linear-gradient(
			135deg,
			var(--accent-primary) 0%,
			color-mix(in oklch, var(--accent-primary) 85%, oklch(0.5 0.15 200)) 100%
		);
		color: var(--primary-foreground, oklch(0.15 0.02 180));
		box-shadow: var(--shadow-sm), var(--shadow-glow-soft);
	}

	.btn-primary:hover:not(:disabled) {
		transform: translateY(-1px);
		box-shadow: var(--shadow-md), var(--shadow-glow);
		filter: brightness(1.08);
	}

	.btn-primary:active:not(:disabled) {
		transform: translateY(0);
		box-shadow: var(--shadow-sm);
	}

	.btn-secondary {
		background: var(--surface-1);
		color: var(--text-primary);
		border: 1px solid var(--border-default);
		box-shadow: var(--shadow-inset), var(--shadow-xs);
	}

	.btn-secondary:hover:not(:disabled) {
		background: var(--surface-2);
		border-color: var(--border-emphasis);
		box-shadow: var(--shadow-inset), var(--shadow-sm);
	}

	.btn-sm {
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
	}

	.icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all var(--transition-fast);
	}

	.icon-btn:hover {
		background: var(--surface-2);
		color: var(--text-primary);
	}

	.icon-btn.danger:hover {
		background: color-mix(in oklch, var(--accent-danger) 12%, transparent);
		color: var(--accent-danger);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   THEME SELECTOR
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.theme-options {
		display: flex;
		gap: var(--space-3);
	}

	.theme-btn {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-4);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: all var(--transition-smooth);
		color: var(--text-primary);
		font-family: var(--font-display);
	}

	.theme-btn:hover {
		background: var(--surface-hover);
		border-color: var(--border-default);
		transform: translateY(-2px);
		box-shadow: var(--shadow-md);
	}

	.theme-btn.active {
		background: color-mix(in oklch, var(--accent-primary) 12%, transparent);
		border-color: var(--accent-primary);
		box-shadow: var(--shadow-sm), var(--shadow-glow-soft);
	}

	.theme-preview {
		width: 48px;
		height: 48px;
		border-radius: var(--radius-md);
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.5rem;
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
	}

	.theme-preview.light {
		background: oklch(0.95 0.005 60);
		border-color: oklch(0.85 0.01 60);
	}

	.theme-preview.dark {
		background: oklch(0.15 0.01 260);
		border-color: oklch(0.25 0.01 260);
	}

	.theme-preview.system {
		background: linear-gradient(135deg, oklch(0.95 0.005 60) 50%, oklch(0.15 0.01 260) 50%);
	}

	.theme-btn span:not(.theme-preview) {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-primary);
	}

	.theme-btn.active span:not(.theme-preview) {
		color: var(--accent-primary);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   TOGGLES
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--surface-2);
		border-radius: var(--radius-full);
		cursor: pointer;
		transition: all var(--transition-smooth);
		border: 1px solid var(--border-subtle);
	}

	.toggle:hover {
		border-color: var(--border-default);
	}

	.toggle.active {
		background: var(--accent-primary);
		border-color: var(--accent-primary);
		box-shadow: var(--shadow-glow-soft);
	}

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: white;
		border-radius: 50%;
		transition: all var(--transition-spring);
		box-shadow: var(--shadow-sm);
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}

	.toggle-sm {
		position: relative;
		width: 36px;
		height: 20px;
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-full);
		cursor: pointer;
		transition: all var(--transition-smooth);
	}

	.toggle-sm.active {
		background: var(--accent-primary);
		border-color: var(--accent-primary);
		box-shadow: var(--shadow-glow-soft);
	}

	.toggle-knob-sm {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 14px;
		height: 14px;
		background: white;
		border-radius: 50%;
		transition: all var(--transition-spring);
		box-shadow: var(--shadow-xs);
	}

	.toggle-sm.active .toggle-knob-sm {
		transform: translateX(16px);
	}

	.toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-4);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   BADGES
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.badge {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 3px 8px;
		font-size: var(--text-xs);
		font-weight: 600;
		letter-spacing: 0.02em;
		border-radius: var(--radius-sm);
		white-space: nowrap;
	}

	.badge.success {
		background: color-mix(in oklch, var(--accent-success) 12%, transparent);
		color: var(--accent-success);
		border: 1px solid color-mix(in oklch, var(--accent-success) 25%, transparent);
	}

	.badge.error {
		background: color-mix(in oklch, var(--accent-danger) 12%, transparent);
		color: var(--accent-danger);
		border: 1px solid color-mix(in oklch, var(--accent-danger) 25%, transparent);
	}

	.badge.muted {
		background: var(--surface-1);
		color: var(--text-secondary);
		border: 1px solid var(--border-subtle);
	}

	.badge.primary {
		background: color-mix(in oklch, var(--accent-primary) 12%, transparent);
		color: var(--accent-primary);
		border: 1px solid color-mix(in oklch, var(--accent-primary) 25%, transparent);
	}

	.badge.warning {
		background: color-mix(in oklch, var(--accent-warning) 12%, transparent);
		color: var(--accent-warning);
		border: 1px solid color-mix(in oklch, var(--accent-warning) 25%, transparent);
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   BANNERS
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.error-banner {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		background: color-mix(in oklch, var(--accent-danger) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-danger) 25%, transparent);
		border-radius: var(--radius-md);
		color: var(--accent-danger);
		font-size: var(--text-sm);
		margin-bottom: var(--space-4);
	}

	.success-banner {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		background: color-mix(in oklch, var(--accent-success) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-success) 25%, transparent);
		border-radius: var(--radius-md);
		color: var(--accent-success);
		font-size: var(--text-sm);
		margin-bottom: var(--space-4);
	}

	.close-btn {
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		font-size: 1.25rem;
		line-height: 1;
		cursor: pointer;
		opacity: 0.7;
		padding: var(--space-1);
		transition: all var(--transition-fast);
		color: inherit;
	}

	.close-btn:hover {
		opacity: 1;
		background: color-mix(in oklch, currentColor 10%, transparent);
	}

	/* ============================================
	   UTILITY CLASSES
	   ============================================ */
	.text-muted { color: var(--text-tertiary); }
	:global(.light) .text-muted { color: var(--text-tertiary); }
	.text-success { color: var(--accent-success); }
	.text-error { color: var(--accent-danger); }
	.text-primary { color: var(--accent-glow); }
	.text-sm { font-size: 0.875rem; }
	.font-medium { font-weight: 500; }
	.font-mono { font-family: var(--font-mono); }
	.flex-1 { flex: 1; }
	.w-full { width: 100%; }

	/* ============================================
	   CONNECTION CARDS - Neo-Glass Style
	   ============================================ */
	.connection-card {
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		padding: var(--space-5);
		margin-bottom: var(--space-4);
		transition: all var(--transition-smooth);
		position: relative;
	}

	.connection-card:hover {
		border-color: var(--border-default);
		box-shadow: var(--shadow-md), var(--shadow-glow-soft);
	}

	/* Connected state */
	.connection-card.connected {
		border-color: color-mix(in oklch, var(--accent-success) 40%, transparent);
		box-shadow: var(--shadow-sm), 0 0 12px color-mix(in oklch, var(--accent-success) 15%, transparent);
	}

	.connection-header {
		display: flex;
		align-items: center;
		gap: var(--space-4);
		margin-bottom: var(--space-4);
	}

	.connection-icon {
		width: 44px;
		height: 44px;
		border-radius: var(--radius-md);
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.25rem;
		position: relative;
		box-shadow: var(--shadow-inset), var(--shadow-xs);
	}

	/* Status indicator on icon */
	.connection-icon::after {
		content: '';
		position: absolute;
		top: -3px;
		right: -3px;
		width: 10px;
		height: 10px;
		border-radius: 50%;
		background: var(--text-tertiary);
		border: 2px solid var(--surface-0);
		transition: all var(--transition-smooth);
	}

	.connection-card.connected .connection-icon::after {
		background: var(--accent-success);
		box-shadow: 0 0 8px color-mix(in oklch, var(--accent-success) 50%, transparent);
	}

	/* Service-specific icon colors */
	.connection-icon.github {
		background: linear-gradient(135deg, oklch(0.20 0.01 250) 0%, oklch(0.15 0.01 250) 100%);
		border-color: oklch(0.30 0.02 250);
		color: oklch(0.95 0 0);
	}

	.connection-icon.claude {
		background: linear-gradient(135deg, oklch(0.22 0.08 25) 0%, oklch(0.16 0.06 25) 100%);
		border-color: oklch(0.35 0.10 25);
	}

	.connection-icon.orange {
		background: linear-gradient(135deg, color-mix(in oklch, var(--accent-warning) 20%, transparent), color-mix(in oklch, var(--accent-warning) 12%, transparent));
		border-color: color-mix(in oklch, var(--accent-warning) 35%, transparent);
	}

	.connection-icon.docker {
		background: linear-gradient(135deg, oklch(0.50 0.15 230) 0%, oklch(0.40 0.12 230) 100%);
		border-color: oklch(0.50 0.14 230);
	}

	.connection-title {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		font-family: var(--font-display);
		font-weight: 600;
		color: var(--text-primary);
	}

	.connection-info p {
		color: var(--text-secondary);
		font-family: var(--font-display);
		font-size: var(--text-sm);
	}

	.connection-actions {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.oauth-flow {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	/* Docker info */
	.docker-info {
		display: flex;
		align-items: flex-start;
		gap: var(--space-4);
		padding: var(--space-4);
		background: color-mix(in oklch, var(--accent-primary) 8%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-primary) 20%, transparent);
		border-radius: var(--radius-md);
	}

	.docker-info p {
		color: var(--text-primary);
		font-family: var(--font-display);
	}

	.docker-info code {
		background: var(--surface-2);
		padding: 2px var(--space-2);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--accent-primary);
	}

	/* Info card */
	.info-card {
		display: flex;
		align-items: center;
		gap: var(--space-4);
		padding: var(--space-4);
		border-radius: var(--radius-md);
	}

	.info-card.success {
		background: color-mix(in oklch, var(--accent-success) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-success) 25%, transparent);
	}

	.info-card .icon {
		font-size: 1.5rem;
	}

	.info-card h4 {
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: 600;
		margin: 0;
		color: var(--text-primary);
	}

	.info-card p {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		margin: 2px 0 0;
	}

	/* ============================================
	   USER LIST - Neo-Glass Style
	   ============================================ */
	.user-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.user-card {
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		padding: var(--space-4);
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: var(--space-4);
		transition: all var(--transition-smooth);
		position: relative;
	}

	.user-card:hover {
		border-color: var(--border-default);
		box-shadow: var(--shadow-sm), var(--shadow-glow-soft);
	}

	/* Active user status */
	.user-card.active {
		border-color: color-mix(in oklch, var(--accent-success) 30%, transparent);
	}

	/* Inactive/disabled user */
	.user-card.inactive {
		opacity: 0.7;
	}

	.user-info {
		flex: 1;
		min-width: 0;
	}

	.user-header {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		flex-wrap: wrap;
	}

	.user-name {
		font-family: var(--font-display);
		font-weight: 600;
		font-size: var(--text-base);
		color: var(--text-primary);
	}

	.user-desc {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		margin: var(--space-2) 0;
		line-height: 1.5;
	}

	.user-meta {
		display: flex;
		gap: var(--space-4);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--text-tertiary);
		letter-spacing: 0.02em;
		text-transform: uppercase;
	}

	.user-meta span {
		display: flex;
		align-items: center;
		gap: var(--space-1);
	}

	.user-actions {
		display: flex;
		gap: var(--space-2);
	}

	.user-actions button {
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		padding: var(--space-2) var(--space-3);
		font-family: var(--font-display);
		font-size: var(--text-xs);
		font-weight: 500;
		color: var(--text-secondary);
		cursor: pointer;
		transition: all var(--transition-fast);
	}

	.user-actions button:hover {
		background: var(--surface-3);
		border-color: var(--border-default);
		color: var(--text-primary);
	}

	.user-actions button.danger:hover {
		background: color-mix(in oklch, var(--accent-danger) 15%, transparent);
		border-color: color-mix(in oklch, var(--accent-danger) 40%, transparent);
		color: var(--accent-danger);
	}

	.loading, .empty-state {
		text-align: center;
		padding: var(--space-8);
		color: var(--text-secondary);
		font-family: var(--font-display);
	}

	/* ============================================
	   API KEY CARDS - Neo-Glass Style
	   ============================================ */
	.api-key-card {
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		padding: var(--space-5);
		margin-bottom: var(--space-4);
		transition: all var(--transition-smooth);
		position: relative;
	}

	.api-key-card:hover {
		border-color: var(--border-default);
		box-shadow: var(--shadow-sm), var(--shadow-glow-soft);
	}

	/* Configured state */
	.api-key-card.configured {
		border-color: color-mix(in oklch, var(--accent-success) 30%, transparent);
	}

	/* Provider-specific hover accents */
	.api-key-card.openai:hover {
		border-color: color-mix(in oklch, var(--accent-success) 40%, transparent);
		box-shadow: var(--shadow-sm), 0 0 12px color-mix(in oklch, var(--accent-success) 15%, transparent);
	}

	.api-key-card.google:hover {
		border-color: color-mix(in oklch, var(--accent-primary) 40%, transparent);
		box-shadow: var(--shadow-sm), 0 0 12px color-mix(in oklch, var(--accent-primary) 15%, transparent);
	}

	.api-key-card.meshy:hover {
		border-color: oklch(0.55 0.18 300 / 0.4);
		box-shadow: var(--shadow-sm), 0 0 12px oklch(0.55 0.18 300 / 0.15);
	}

	.api-key-header {
		display: flex;
		align-items: center;
		gap: var(--space-4);
		margin-bottom: var(--space-4);
	}

	.api-icon {
		width: 44px;
		height: 44px;
		border-radius: var(--radius-md);
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.25rem;
		border: 1px solid transparent;
		position: relative;
		box-shadow: var(--shadow-inset), var(--shadow-xs);
	}

	/* Secured indicator on icon */
	.api-icon.secured::after {
		content: '';
		position: absolute;
		bottom: -3px;
		right: -3px;
		width: 12px;
		height: 12px;
		border-radius: 50%;
		background: var(--accent-success);
		border: 2px solid var(--surface-0);
		box-shadow: 0 0 6px color-mix(in oklch, var(--accent-success) 50%, transparent);
	}

	.api-icon.openai {
		background: linear-gradient(135deg, color-mix(in oklch, var(--accent-success) 18%, transparent), color-mix(in oklch, var(--accent-success) 10%, transparent));
		border-color: color-mix(in oklch, var(--accent-success) 30%, transparent);
	}

	.api-icon.google {
		background: linear-gradient(135deg, color-mix(in oklch, var(--accent-primary) 18%, transparent), color-mix(in oklch, var(--accent-primary) 10%, transparent));
		border-color: color-mix(in oklch, var(--accent-primary) 30%, transparent);
	}

	.api-icon.meshy {
		background: linear-gradient(135deg, oklch(0.55 0.18 300 / 0.18), oklch(0.55 0.18 300 / 0.10));
		border-color: oklch(0.55 0.18 300 / 0.30);
	}

	.api-icon.elevenlabs {
		background: linear-gradient(135deg, oklch(0.60 0.14 45 / 0.18), oklch(0.60 0.14 45 / 0.10));
		border-color: oklch(0.60 0.14 45 / 0.30);
	}

	.api-icon.anthropic {
		background: linear-gradient(135deg, oklch(0.55 0.14 25 / 0.18), oklch(0.55 0.14 25 / 0.10));
		border-color: oklch(0.55 0.14 25 / 0.30);
	}

	.api-title {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		font-family: var(--font-display);
		font-weight: 600;
		color: var(--text-primary);
	}

	.api-key-form {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	/* Masked key display styling */
	.api-key-masked {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		letter-spacing: 0.08em;
		color: var(--text-tertiary);
		background: var(--surface-2);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-sm);
		border: 1px solid var(--border-subtle);
	}

	/* ============================================
	   MODEL GRID / CARDS - Neo-Glass Style
	   ============================================ */
	.model-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
		gap: var(--space-3);
	}

	.model-btn {
		display: flex;
		flex-direction: column;
		padding: var(--space-3);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: all var(--transition-fast);
		text-align: left;
		font-family: var(--font-display);
	}

	.model-btn:hover:not(:disabled) {
		border-color: color-mix(in oklch, var(--accent-primary) 50%, transparent);
		background: var(--surface-2);
		box-shadow: var(--shadow-sm);
	}

	.model-btn.active {
		border-color: var(--accent-primary);
		background: color-mix(in oklch, var(--accent-primary) 10%, transparent);
		box-shadow: 0 0 0 1px color-mix(in oklch, var(--accent-primary) 20%, transparent);
	}

	.model-btn.disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.model-name {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		font-weight: 600;
		color: var(--text-primary);
	}

	.model-price {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--accent-primary);
		margin-top: auto;
		padding-top: var(--space-2);
	}

	.model-provider {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--text-tertiary);
		text-transform: uppercase;
		letter-spacing: 0.02em;
	}

	/* Audio models */
	.audio-models {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-4);
	}

	.audio-model-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.audio-btn {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: var(--space-3) var(--space-4);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		cursor: pointer;
		font-family: var(--font-display);
		font-size: var(--text-sm);
		transition: all var(--transition-fast);
	}

	.audio-btn:hover:not(:disabled) {
		border-color: color-mix(in oklch, var(--accent-primary) 50%, transparent);
		box-shadow: var(--shadow-xs);
	}

	.audio-btn.active {
		border-color: var(--accent-primary);
		background: color-mix(in oklch, var(--accent-primary) 10%, transparent);
	}

	.audio-btn .price {
		font-family: var(--font-mono);
		color: var(--accent-primary);
		font-size: var(--text-xs);
	}

	/* ============================================
	   CLEANUP SECTION - Neo-Glass Style
	   ============================================ */
	.cleanup-status {
		display: flex;
		align-items: center;
		gap: var(--space-4);
		padding: var(--space-4) var(--space-5);
		border-radius: var(--radius-lg);
		margin-bottom: var(--space-5);
	}

	.cleanup-status.active {
		background: color-mix(in oklch, var(--accent-success) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-success) 25%, transparent);
	}

	.cleanup-status.sleeping {
		background: color-mix(in oklch, var(--accent-warning) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-warning) 25%, transparent);
	}

	.cleanup-status p {
		color: var(--text-primary);
		font-family: var(--font-display);
		font-weight: 500;
	}

	.status-icon {
		font-size: 1.5rem;
	}

	.cleanup-inputs {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-4);
		margin-top: var(--space-4);
	}

	.cleanup-inputs.three-col {
		grid-template-columns: repeat(3, 1fr);
	}

	.cleanup-inputs label {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		font-weight: 500;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--text-secondary);
		display: block;
		margin-bottom: var(--space-2);
	}

	.cleanup-items {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.cleanup-item {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: var(--space-4);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		color: var(--text-primary);
		font-family: var(--font-display);
		font-weight: 500;
		transition: all var(--transition-smooth);
	}

	.cleanup-item:hover {
		border-color: var(--border-default);
		box-shadow: var(--shadow-xs);
	}

	.cleanup-controls {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}

	.cleanup-controls .toggle-sm {
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
		box-shadow: var(--shadow-inset);
	}

	.cleanup-controls .toggle-sm.active {
		background: var(--accent-primary);
		box-shadow: 0 0 8px color-mix(in oklch, var(--accent-primary) 40%, transparent);
	}

	.cleanup-actions {
		display: flex;
		gap: var(--space-4);
		margin-top: var(--space-5);
	}

	/* Cleanup Item Label with Preview Count */
	.cleanup-item-label {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}

	.preview-count {
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--accent-warning);
		background: color-mix(in oklch, var(--accent-warning) 15%, transparent);
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-sm);
		animation: fadeIn 0.2s ease-out;
	}

	@keyframes fadeIn {
		from { opacity: 0; transform: scale(0.9); }
		to { opacity: 1; transform: scale(1); }
	}

	/* Inline Preview Summary */
	.cleanup-preview-inline {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--space-3) var(--space-4);
		border-radius: var(--radius-md);
		margin-bottom: var(--space-4);
		font-size: var(--text-sm);
		animation: fadeIn 0.2s ease-out;
	}

	.cleanup-preview-inline.no-files {
		background: color-mix(in oklch, var(--accent-success) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-success) 25%, transparent);
	}

	.cleanup-preview-inline.has-files {
		background: color-mix(in oklch, var(--accent-warning) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-warning) 25%, transparent);
	}

	.cleanup-preview-inline .preview-status {
		color: var(--text-primary);
		font-family: var(--font-display);
	}

	.cleanup-preview-inline .preview-clear {
		background: none;
		border: none;
		color: var(--text-muted);
		cursor: pointer;
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-sm);
		border-radius: var(--radius-sm);
		transition: all var(--transition-smooth);
	}

	.cleanup-preview-inline .preview-clear:hover {
		background: var(--surface-2);
		color: var(--text-primary);
	}

	.btn-danger {
		background: linear-gradient(135deg,
			color-mix(in oklch, var(--accent-danger) 15%, transparent),
			color-mix(in oklch, var(--accent-danger) 8%, transparent));
		border: 1px solid color-mix(in oklch, var(--accent-danger) 40%, transparent);
		color: var(--accent-danger);
	}

	.btn-danger:hover:not(:disabled) {
		background: color-mix(in oklch, var(--accent-danger) 20%, transparent);
		border-color: var(--accent-danger);
		box-shadow: 0 0 12px color-mix(in oklch, var(--accent-danger) 30%, transparent);
	}

	/* Key display - Neo-Glass Style */
	.key-display {
		background: color-mix(in oklch, var(--accent-success) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--accent-success) 25%, transparent);
		border-radius: var(--radius-lg);
		padding: var(--space-4);
		margin-top: var(--space-4);
	}

	.key-display p {
		color: var(--text-primary);
		font-family: var(--font-display);
		font-weight: 500;
	}

	.key-display code {
		background: var(--surface-2);
		padding: var(--space-3) var(--space-4);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		word-break: break-all;
		color: var(--accent-success);
		display: block;
		margin-top: var(--space-2);
	}

	/* ============================================
	   MOBILE STYLES - Neo-Glass
	   ============================================ */
	.mobile-header {
		padding: var(--space-4);
		border-bottom: 1px solid var(--border-subtle);
		background: var(--surface-1);
	}

	.menu-toggle {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		background: none;
		border: none;
		color: var(--text-primary);
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: 600;
		cursor: pointer;
	}

	.mobile-menu {
		padding: var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
		background: var(--surface-1);
	}

	.mobile-category {
		margin-bottom: var(--space-2);
	}

	.mobile-category .category-btn {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		padding: var(--space-3) var(--space-4);
		background: transparent;
		border: none;
		color: var(--text-secondary);
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: 600;
		border-radius: var(--radius-sm);
		cursor: pointer;
		text-align: left;
	}

	.mobile-category .category-btn.active {
		color: var(--accent-primary);
		background: color-mix(in oklch, var(--accent-primary) 10%, transparent);
	}

	.mobile-sections {
		margin-left: var(--space-6);
		padding: var(--space-2) 0;
	}

	.mobile-content {
		flex: 1;
		overflow-y: auto;
		padding: var(--space-5);
	}

	/* ============================================
	   MODELS SECTION - Neo-Glass
	   ============================================ */
	.models-section {
		max-width: 860px;
	}

	.group-description {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		margin-bottom: var(--space-5);
		line-height: 1.5;
	}

	.provider-section {
		margin-bottom: var(--space-5);
	}

	.provider-section:last-child {
		margin-bottom: 0;
	}

	.provider-header {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		font-weight: 500;
		color: var(--text-tertiary);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin-bottom: var(--space-3);
		padding-bottom: var(--space-2);
		border-bottom: 1px solid var(--border-subtle);
	}

	.model-cards {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
		gap: var(--space-3);
	}

	.model-cards.tts-cards,
	.model-cards.stt-cards {
		grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
	}

	.model-card {
		display: flex;
		flex-direction: column;
		padding: var(--space-4);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		cursor: pointer;
		transition: all var(--transition-smooth);
		text-align: left;
		gap: var(--space-2);
		box-shadow: var(--shadow-inset);
	}

	.model-card:hover:not(:disabled) {
		border-color: color-mix(in oklch, var(--accent-primary) 50%, transparent);
		background: var(--surface-2);
		box-shadow: var(--shadow-sm);
	}

	.model-card.active {
		border-color: var(--accent-primary);
		background: color-mix(in oklch, var(--accent-primary) 10%, transparent);
		box-shadow: 0 0 0 1px color-mix(in oklch, var(--accent-primary) 30%, transparent),
			var(--shadow-glow-soft);
	}

	.model-card.deprecated {
		opacity: 0.65;
	}

	.model-card.deprecated:hover {
		border-color: color-mix(in oklch, var(--accent-warning) 50%, transparent);
	}

	.model-card:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.model-card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		position: relative;
	}

	.model-card-name {
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
		transition: color var(--transition-fast);
	}

	.model-card.active .model-card-name {
		color: var(--accent-primary);
	}

	.model-card-desc {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		line-height: 1.5;
		margin: 0;
		max-height: 3em;
		overflow: hidden;
		transition: max-height var(--transition-smooth);
	}

	.model-card:hover .model-card-desc {
		max-height: 6em;
	}

	.model-card-capabilities {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-1);
		margin-top: var(--space-2);
	}

	.capability-badge {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		font-weight: 500;
		padding: 2px var(--space-2);
		border-radius: var(--radius-sm);
		background: var(--surface-2);
		color: var(--text-tertiary);
		letter-spacing: 0.02em;
		text-transform: uppercase;
		border: 1px solid var(--border-subtle);
		transition: all var(--transition-fast);
	}

	.model-card:hover .capability-badge {
		border-color: var(--border-default);
	}

	.capability-badge.highlight {
		background: color-mix(in oklch, var(--accent-primary) 15%, transparent);
		color: var(--accent-primary);
		border-color: color-mix(in oklch, var(--accent-primary) 25%, transparent);
	}

	.capability-badge.audio {
		background: oklch(0.60 0.15 300 / 0.15);
		color: oklch(0.70 0.18 300);
		border-color: oklch(0.60 0.15 300 / 0.25);
	}

	.capability-badge.text-excellent {
		background: color-mix(in oklch, var(--accent-success) 15%, transparent);
		color: var(--accent-success);
		border-color: color-mix(in oklch, var(--accent-success) 25%, transparent);
	}

	.capability-badge.text-good {
		background: color-mix(in oklch, var(--accent-primary) 15%, transparent);
		color: var(--accent-primary);
		border-color: color-mix(in oklch, var(--accent-primary) 25%, transparent);
	}

	.capability-badge.text-basic {
		background: color-mix(in oklch, var(--accent-warning) 15%, transparent);
		color: var(--accent-warning);
		border-color: color-mix(in oklch, var(--accent-warning) 25%, transparent);
	}

	.model-card-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-top: auto;
		padding-top: var(--space-3);
		border-top: 1px solid var(--border-subtle);
	}

	.model-card-price {
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		font-weight: 600;
		color: var(--accent-primary);
	}

	.model-card-resolution {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--text-tertiary);
	}

	.model-card-formats {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--text-tertiary);
		text-transform: uppercase;
		letter-spacing: 0.02em;
	}

	.badge.warning {
		background: color-mix(in oklch, var(--accent-warning) 15%, transparent);
		color: var(--accent-warning);
	}

	/* ============================================
	   TTS SETTINGS - Neo-Glass Style
	   ============================================ */
	.tts-settings {
		margin-top: var(--space-5);
		padding: var(--space-5);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-5);
	}

	.tts-setting-row {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.tts-setting-row label {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		font-weight: 500;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--text-secondary);
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.tts-setting-row label span {
		font-weight: 600;
		color: var(--accent-primary);
		font-size: var(--text-sm);
	}

	/* Slider */
	.slider {
		width: 100%;
		height: 6px;
		-webkit-appearance: none;
		appearance: none;
		background: var(--surface-2);
		border-radius: var(--radius-sm);
		outline: none;
		border: 1px solid var(--border-subtle);
	}

	.slider::-webkit-slider-thumb {
		-webkit-appearance: none;
		appearance: none;
		width: 18px;
		height: 18px;
		border-radius: 50%;
		background: var(--accent-primary);
		cursor: pointer;
		border: 2px solid var(--surface-0);
		box-shadow: var(--shadow-sm);
		transition: all var(--transition-fast);
	}

	.slider::-webkit-slider-thumb:hover {
		transform: scale(1.1);
		box-shadow: var(--shadow-md), var(--shadow-glow-soft);
	}

	.slider::-moz-range-thumb {
		width: 18px;
		height: 18px;
		border-radius: 50%;
		background: var(--accent-primary);
		cursor: pointer;
		border: 2px solid var(--surface-0);
		box-shadow: var(--shadow-sm);
	}

	.tts-settings select.input {
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		font-weight: 500;
	}

	.tts-settings select.input:focus {
		border-color: var(--accent-primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent-primary) 15%, transparent);
	}

	/* ============================================
	   RESPONSIVE / MOBILE
	   ============================================ */
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

		.settings-content {
			padding: 16px;
		}

		.section-content {
			max-width: 100%;
		}
	}

	/* ═══════════════════════════════════════════════════════════════════════════════
	   API USERS SECTION - Inline Form & Merged Policies
	   ═══════════════════════════════════════════════════════════════════════════════ */

	.api-users-section {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.api-users-section .section-header {
		flex-shrink: 0;
	}

	.section-title-row {
		flex: 1;
	}

	/* Scrollable content area */
	.api-users-scroll-area {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		padding-right: var(--space-2);
		margin-right: calc(var(--space-2) * -1);
	}

	/* Custom scrollbar for scroll area */
	.api-users-scroll-area::-webkit-scrollbar {
		width: 6px;
	}

	.api-users-scroll-area::-webkit-scrollbar-track {
		background: transparent;
	}

	.api-users-scroll-area::-webkit-scrollbar-thumb {
		background: var(--border-subtle);
		border-radius: var(--radius-full);
	}

	.api-users-scroll-area::-webkit-scrollbar-thumb:hover {
		background: var(--border-default);
	}

	/* Glow button effect */
	.btn-glow {
		position: relative;
		overflow: hidden;
	}

	.btn-glow::before {
		content: '';
		position: absolute;
		inset: 0;
		background: linear-gradient(
			135deg,
			transparent 0%,
			color-mix(in oklch, white 20%, transparent) 50%,
			transparent 100%
		);
		transform: translateX(-100%);
		transition: transform 0.6s ease;
	}

	.btn-glow:hover::before {
		transform: translateX(100%);
	}

	.btn-icon {
		font-size: 1.1em;
		font-weight: 300;
		margin-right: var(--space-2);
	}

	/* ────────────────────────────────────────────────────────────────
	   Compact Key Policies Grid
	   ──────────────────────────────────────────────────────────────── */

	.policies-compact {
		margin-bottom: var(--space-5);
		background: linear-gradient(
			135deg,
			color-mix(in oklch, var(--surface-1) 80%, var(--accent-primary) 3%),
			var(--surface-1)
		);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		overflow: hidden;
	}

	.policies-toggle {
		width: 100%;
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-4);
		background: transparent;
		border: none;
		cursor: pointer;
		font-family: var(--font-display);
		color: var(--text-primary);
		transition: all var(--transition-smooth);
	}

	.policies-toggle:hover {
		background: var(--surface-hover);
	}

	.toggle-icon {
		font-size: 0.75rem;
		color: var(--text-tertiary);
		transition: transform var(--transition-spring);
	}

	.toggle-icon.rotated {
		transform: rotate(90deg);
	}

	.toggle-label {
		font-weight: 600;
		font-size: var(--text-base);
	}

	.policies-summary {
		margin-left: auto;
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--text-tertiary);
		padding: var(--space-1) var(--space-3);
		background: var(--surface-2);
		border-radius: var(--radius-full);
	}

	.policies-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: var(--space-3);
		padding: 0 var(--space-4) var(--space-4);
	}

	@media (min-width: 600px) {
		.policies-grid {
			grid-template-columns: repeat(4, 1fr);
		}
	}

	.policy-compact-item {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		padding: var(--space-3);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		transition: all var(--transition-smooth);
		animation: fadeSlideUp 0.3s ease-out backwards;
	}

	@keyframes fadeSlideUp {
		from {
			opacity: 0;
			transform: translateY(8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.policy-compact-item:hover {
		border-color: var(--border-default);
		box-shadow: var(--shadow-sm);
	}

	.policy-compact-item.status-green {
		border-left: 2px solid var(--accent-success);
	}

	.policy-compact-item.status-yellow {
		border-left: 2px solid var(--accent-warning);
	}

	.policy-compact-item.status-red {
		border-left: 2px solid var(--accent-danger);
	}

	.policy-compact-item.status-blue {
		border-left: 2px solid var(--accent-info);
	}

	.policy-compact-header {
		display: flex;
		align-items: center;
		gap: var(--space-2);
	}

	.status-indicator {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--text-tertiary);
		flex-shrink: 0;
	}

	.policy-compact-item.status-green .status-indicator {
		background: var(--accent-success);
		box-shadow: 0 0 6px color-mix(in oklch, var(--accent-success) 50%, transparent);
	}

	.policy-compact-item.status-yellow .status-indicator {
		background: var(--accent-warning);
		box-shadow: 0 0 6px color-mix(in oklch, var(--accent-warning) 50%, transparent);
	}

	.policy-compact-item.status-red .status-indicator {
		background: var(--accent-danger);
		box-shadow: 0 0 6px color-mix(in oklch, var(--accent-danger) 50%, transparent);
	}

	.policy-compact-item.status-blue .status-indicator {
		background: var(--accent-info);
		box-shadow: 0 0 6px color-mix(in oklch, var(--accent-info) 50%, transparent);
	}

	.policy-name {
		font-family: var(--font-display);
		font-size: var(--text-xs);
		font-weight: 600;
		color: var(--text-primary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.policy-select-compact {
		width: 100%;
		padding: var(--space-2) var(--space-3);
		font-family: var(--font-mono);
		font-size: 10px;
		font-weight: 500;
		border-radius: var(--radius-sm);
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all var(--transition-fast);
		appearance: none;
		-webkit-appearance: none;
		background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%236b7a99' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.5' d='M6 8l4 4 4-4'/%3e%3c/svg%3e");
		background-position: right 0.5rem center;
		background-repeat: no-repeat;
		background-size: 1em 1em;
		padding-right: 2rem;
	}

	.policy-select-compact:hover {
		border-color: var(--border-default);
	}

	.policy-select-compact:focus {
		outline: none;
		border-color: var(--accent-primary);
	}

	.saving-dot {
		position: absolute;
		top: var(--space-2);
		right: var(--space-2);
		width: 8px;
		height: 8px;
		background: var(--accent-primary);
		border-radius: 50%;
		animation: pulse 1s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; transform: scale(1); }
		50% { opacity: 0.5; transform: scale(0.8); }
	}

	/* ────────────────────────────────────────────────────────────────
	   Inline Form Panel
	   ──────────────────────────────────────────────────────────────── */

	.inline-form-panel {
		margin-bottom: var(--space-5);
		background: linear-gradient(
			145deg,
			color-mix(in oklch, var(--accent-primary) 5%, var(--surface-1)),
			var(--surface-1) 60%
		);
		border: 1px solid color-mix(in oklch, var(--accent-primary) 20%, var(--border-subtle));
		border-radius: var(--radius-xl);
		padding: var(--space-5);
		position: relative;
		overflow: hidden;
	}

	/* Decorative gradient line at top */
	.inline-form-panel::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 2px;
		background: linear-gradient(
			90deg,
			var(--accent-primary) 0%,
			color-mix(in oklch, var(--accent-primary) 50%, var(--accent-info)) 50%,
			var(--accent-info) 100%
		);
	}

	.form-panel-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: var(--space-5);
	}

	.form-panel-header h4 {
		font-family: var(--font-display);
		font-size: var(--text-lg);
		font-weight: 700;
		color: var(--text-primary);
		margin: 0;
		background: linear-gradient(135deg, var(--text-primary), var(--accent-primary));
		-webkit-background-clip: text;
		-webkit-text-fill-color: transparent;
		background-clip: text;
	}

	.close-form {
		opacity: 0.6;
		transition: all var(--transition-fast);
	}

	.close-form:hover {
		opacity: 1;
		transform: rotate(90deg);
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-4);
	}

	.form-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.form-field.full-width {
		grid-column: 1 / -1;
	}

	.form-field label {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		font-weight: 600;
		color: var(--text-secondary);
	}

	.required {
		color: var(--accent-danger);
	}

	.input-lg {
		font-size: var(--text-md);
		padding: var(--space-4);
	}

	.field-hint {
		font-size: var(--text-xs);
		color: var(--text-tertiary);
		margin: 0;
	}

	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-3);
		margin-top: var(--space-5);
		padding-top: var(--space-4);
		border-top: 1px solid var(--border-subtle);
	}

	/* ────────────────────────────────────────────────────────────────
	   Success State - Key Display
	   ──────────────────────────────────────────────────────────────── */

	.key-success-panel {
		text-align: center;
		padding: var(--space-5);
	}

	.success-icon {
		font-size: 3rem;
		margin-bottom: var(--space-4);
		animation: bounceIn 0.5s ease-out;
	}

	@keyframes bounceIn {
		0% { transform: scale(0); opacity: 0; }
		50% { transform: scale(1.2); }
		100% { transform: scale(1); opacity: 1; }
	}

	.key-success-panel h5 {
		font-family: var(--font-display);
		font-size: var(--text-lg);
		font-weight: 700;
		color: var(--accent-success);
		margin: 0 0 var(--space-2) 0;
	}

	.key-warning {
		font-size: var(--text-sm);
		color: var(--accent-warning);
		margin-bottom: var(--space-4);
	}

	.key-display-inline {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		align-items: center;
	}

	.key-display-inline code {
		display: block;
		width: 100%;
		padding: var(--space-4);
		background: var(--surface-0);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--accent-success);
		word-break: break-all;
		text-align: left;
	}

	/* ────────────────────────────────────────────────────────────────
	   Enhanced User Cards
	   ──────────────────────────────────────────────────────────────── */

	.user-card {
		animation: fadeSlideUp 0.3s ease-out backwards;
	}

	.user-card.editing {
		border-color: var(--accent-primary);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--accent-primary) 20%, transparent);
	}

	/* ────────────────────────────────────────────────────────────────
	   Atmospheric Empty State
	   ──────────────────────────────────────────────────────────────── */

	.empty-state.atmospheric {
		padding: var(--space-8);
		background: linear-gradient(
			145deg,
			color-mix(in oklch, var(--accent-primary) 3%, transparent),
			transparent
		);
		border: 1px dashed var(--border-subtle);
		border-radius: var(--radius-xl);
		text-align: center;
	}

	.empty-icon {
		font-size: 3rem;
		opacity: 0.5;
		margin-bottom: var(--space-4);
	}

	.empty-title {
		font-family: var(--font-display);
		font-size: var(--text-lg);
		font-weight: 600;
		color: var(--text-primary);
		margin: 0 0 var(--space-2) 0;
	}

	.empty-desc {
		color: var(--text-tertiary);
		margin: 0;
	}

	/* ────────────────────────────────────────────────────────────────
	   Loading State
	   ──────────────────────────────────────────────────────────────── */

	.loading-state {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: var(--space-3);
		padding: var(--space-8);
		color: var(--text-secondary);
	}

	.loading-spinner {
		width: 20px;
		height: 20px;
		border: 2px solid var(--border-subtle);
		border-top-color: var(--accent-primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	/* Floating key display for regenerated keys */
	.key-display.floating {
		position: sticky;
		bottom: var(--space-4);
		margin-top: var(--space-4);
		box-shadow: var(--shadow-lg), var(--shadow-glow);
	}

	/* ────────────────────────────────────────────────────────────────
	   Per-User Credential Policies (Below Card)
	   ──────────────────────────────────────────────────────────────── */

	.user-card-main {
		display: flex;
		flex-direction: column;
	}

	.user-policies-below {
		margin-top: var(--space-3);
		padding: var(--space-4);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-lg);
		border-left: 3px solid var(--accent-primary);
	}

	.policies-below-header {
		display: flex;
		align-items: baseline;
		gap: var(--space-3);
		margin-bottom: var(--space-4);
		padding-bottom: var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
	}

	.policies-below-title {
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: 600;
		color: var(--text-primary);
	}

	.policies-below-hint {
		font-size: var(--text-xs);
		color: var(--text-tertiary);
	}

	.policies-loading {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: var(--space-2);
		padding: var(--space-4);
		color: var(--text-secondary);
		font-size: var(--text-sm);
	}

	.loading-spinner.small {
		width: 14px;
		height: 14px;
		border-width: 1.5px;
	}

	.policies-below-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.policy-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-4);
		padding: var(--space-3) var(--space-4);
		background: var(--surface-2);
		border-radius: var(--radius-md);
		transition: all var(--transition-fast);
	}

	.policy-row:hover {
		background: var(--surface-3);
	}

	.policy-row.has-override {
		background: color-mix(in oklch, var(--accent-primary) 8%, var(--surface-2));
		border: 1px solid color-mix(in oklch, var(--accent-primary) 20%, transparent);
	}

	.policy-row-info {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		min-width: 0;
		flex: 1;
	}

	.policy-row-name {
		font-family: var(--font-display);
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-primary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.policy-custom-badge {
		font-size: 10px;
		font-family: var(--font-mono);
		font-weight: 600;
		padding: 2px 8px;
		border-radius: var(--radius-full);
		background: color-mix(in oklch, var(--accent-primary) 15%, transparent);
		color: var(--accent-primary);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		flex-shrink: 0;
	}

	.policy-row-control {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		flex-shrink: 0;
	}

	.policy-row-select {
		min-width: 160px;
		padding: var(--space-2) var(--space-3);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		font-weight: 500;
		border-radius: var(--radius-md);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all var(--transition-fast);
		appearance: none;
		-webkit-appearance: none;
		background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%236b7a99' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.5' d='M6 8l4 4 4-4'/%3e%3c/svg%3e");
		background-position: right 0.75rem center;
		background-repeat: no-repeat;
		background-size: 1em 1em;
		padding-right: 2.5rem;
	}

	.policy-row-select:hover {
		border-color: var(--border-default);
		background-color: var(--surface-2);
	}

	.policy-row-select:focus {
		outline: none;
		border-color: var(--accent-primary);
	}

	.saving-indicator {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--accent-primary);
		animation: pulse 1s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 0.4; transform: scale(0.8); }
		50% { opacity: 1; transform: scale(1); }
	}

	.policies-empty {
		text-align: center;
		padding: var(--space-4);
		color: var(--text-tertiary);
		font-size: var(--text-sm);
	}

	/* Active state for the key policies toggle button */
	.icon-btn.active {
		background: color-mix(in oklch, var(--accent-primary) 15%, transparent);
		color: var(--accent-primary);
	}

	.icon-btn.active:hover {
		background: color-mix(in oklch, var(--accent-primary) 25%, transparent);
	}

	/* ────────────────────────────────────────────────────────────────
	   Profile Multi-Select
	   ──────────────────────────────────────────────────────────────── */

	.profile-multiselect {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		padding: var(--space-3);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		max-height: 180px;
		overflow-y: auto;
	}

	.profile-checkbox-item {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-3);
		background: var(--surface-2);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-full);
		cursor: pointer;
		transition: all var(--transition-fast);
		user-select: none;
	}

	.profile-checkbox-item:hover {
		border-color: var(--accent-primary);
		background: color-mix(in oklch, var(--accent-primary) 5%, var(--surface-2));
	}

	.profile-checkbox-item:has(input:checked) {
		background: color-mix(in oklch, var(--accent-primary) 15%, var(--surface-2));
		border-color: var(--accent-primary);
	}

	.profile-checkbox-item input[type="checkbox"] {
		width: 14px;
		height: 14px;
		accent-color: var(--accent-primary);
		cursor: pointer;
	}

	.profile-checkbox-item .profile-name {
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--text-primary);
		white-space: nowrap;
	}

	.empty-profiles-hint {
		width: 100%;
		text-align: center;
		color: var(--text-tertiary);
		font-size: var(--text-sm);
		margin: 0;
	}
</style>
