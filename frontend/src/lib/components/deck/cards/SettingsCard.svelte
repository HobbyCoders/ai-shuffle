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
		getCredentialPoliciesSummary,
		updateCredentialPolicy,
		getPolicyLabel,
		getEffectiveStatusLabel,
		getEffectiveStatusColor,
		type CredentialPolicySummary,
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
				{ id: 'credential-policies', label: 'Key Policies' },
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

	// Credential policies state
	let credentialPolicies: CredentialPolicySummary[] = $state([]);
	let loadingPolicies = $state(false);
	let savingPolicy = $state<string | null>(null);
	let policyError = $state('');

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
			loadCleanupSettings(),
			loadCredentialPolicies()
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

	async function loadCredentialPolicies() {
		loadingPolicies = true;
		policyError = '';
		try {
			const response = await getCredentialPoliciesSummary();
			credentialPolicies = response.policies;
		} catch (e: any) {
			policyError = e.detail || 'Failed to load credential policies';
		}
		loadingPolicies = false;
	}

	async function handlePolicyChange(policyId: string, newPolicy: CredentialPolicyType) {
		savingPolicy = policyId;
		policyError = '';
		try {
			await updateCredentialPolicy(policyId, newPolicy);
			await loadCredentialPolicies();
		} catch (e: any) {
			policyError = e.detail || 'Failed to update policy';
		}
		savingPolicy = null;
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
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
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
									{#if user.web_login_allowed}
										<span class="badge info" title="Can login via web with username/password">🌐 Web</span>
									{/if}
								</div>
								{#if user.description}
									<p class="user-desc">{user.description}</p>
								{/if}
								<div class="user-meta">
									<span>Profile: {profiles.find(p => p.id === user.profile_id)?.name || 'Any'}</span>
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

	<!-- API & USERS > CREDENTIAL POLICIES -->
	{#if activeSection === 'credential-policies'}
		<div class="section-content">
			<div class="section-header">
				<h3>Key Policies</h3>
				<p>Control which API keys are admin-provided vs user-provided.</p>
			</div>

			{#if loadingPolicies}
				<div class="loading-state">Loading policies...</div>
			{:else if policyError}
				<div class="error-state">{policyError}</div>
			{:else if credentialPolicies.length === 0}
				<div class="empty-state">
					<p>No credential types configured.</p>
				</div>
			{:else}
				<div class="policies-list">
					{#each credentialPolicies as policy (policy.id)}
						<div class="policy-item">
							<div class="policy-info">
								<h4>{policy.name}</h4>
								<p class="policy-description">{policy.description}</p>
								<div class="policy-status" class:status-green={getEffectiveStatusColor(policy.effective_status) === 'green'}
									 class:status-yellow={getEffectiveStatusColor(policy.effective_status) === 'yellow'}
									 class:status-red={getEffectiveStatusColor(policy.effective_status) === 'red'}
									 class:status-blue={getEffectiveStatusColor(policy.effective_status) === 'blue'}>
									<span class="status-dot"></span>
									<span class="status-text">{getEffectiveStatusLabel(policy.effective_status)}</span>
								</div>
							</div>
							<div class="policy-control">
								<select
									value={policy.policy}
									onchange={(e) => handlePolicyChange(policy.id, (e.target as HTMLSelectElement).value as CredentialPolicyType)}
									disabled={savingPolicy === policy.id}
								>
									<option value="admin_provided">Admin Provides</option>
									<option value="user_provided">User Must Provide</option>
									<option value="optional">Optional (User Override)</option>
								</select>
								{#if savingPolicy === policy.id}
									<span class="saving-indicator">Saving...</span>
								{/if}
							</div>
						</div>
					{/each}
				</div>

				<div class="policy-help">
					<h4>Policy Explanations</h4>
					<ul>
						<li><strong>Admin Provides:</strong> All API users will use the admin-configured key. Users cannot override.</li>
						<li><strong>User Must Provide:</strong> Each API user must configure their own key in "My Settings".</li>
						<li><strong>Optional:</strong> Admin key is used by default, but users can override with their own key.</li>
					</ul>
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
						<div class="checkbox-row">
							<label class="checkbox-label">
								<input type="checkbox" bind:checked={formData.web_login_allowed} />
								<span>Allow web login</span>
							</label>
							<p class="text-muted text-xs">User can register with the API key and set a username/password for web access</p>
						</div>
						{#if editingUser?.username}
							<div class="info-box">
								<p class="text-sm"><strong>Registered as:</strong> @{editingUser.username}</p>
								<p class="text-xs text-muted">User has set up web login credentials</p>
							</div>
						{/if}
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
	/* ============================================
	   COMMAND CENTER / INDUSTRIAL CONTROL PANEL
	   A bold, distinctive settings aesthetic
	   ============================================ */

	/* Font imports */
	@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

	/* Keyframe Animations */
	@keyframes fadeSlideIn {
		from {
			opacity: 0;
			transform: translateY(8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	@keyframes pulseGlow {
		0%, 100% {
			box-shadow: 0 0 0 0 oklch(0.65 0.18 250 / 0);
		}
		50% {
			box-shadow: 0 0 12px 2px oklch(0.65 0.18 250 / 0.3);
		}
	}

	@keyframes scanline {
		0% {
			background-position: 0 0;
		}
		100% {
			background-position: 0 4px;
		}
	}

	@keyframes statusPulse {
		0%, 100% {
			opacity: 1;
			box-shadow: 0 0 4px 1px currentColor;
		}
		50% {
			opacity: 0.6;
			box-shadow: 0 0 8px 3px currentColor;
		}
	}

	@keyframes subtleGlow {
		0%, 100% {
			box-shadow: 0 0 0 0 oklch(0.65 0.18 250 / 0);
		}
		50% {
			box-shadow: 0 0 15px 3px oklch(0.65 0.18 250 / 0.25);
		}
	}

	@keyframes terminalBlink {
		0%, 49% {
			opacity: 1;
		}
		50%, 100% {
			opacity: 0;
		}
	}

	@keyframes dataFlow {
		0% {
			background-position: 200% center;
		}
		100% {
			background-position: -200% center;
		}
	}

	@keyframes connectionPulse {
		0%, 100% {
			transform: scale(1);
			opacity: 0.8;
		}
		50% {
			transform: scale(1.3);
			opacity: 1;
		}
	}

	@keyframes bannerGlow {
		0%, 100% {
			box-shadow:
				0 0 0 0 currentColor,
				inset 0 0 20px -10px currentColor;
		}
		50% {
			box-shadow:
				0 0 20px -5px currentColor,
				inset 0 0 30px -10px currentColor;
		}
	}

	/* Root Container with CSS Custom Properties */
	.settings-card-content {
		/* Panel depth system */
		--panel-depth-1: oklch(0.10 0.008 250);
		--panel-depth-2: oklch(0.13 0.010 250);
		--panel-depth-3: oklch(0.16 0.012 250);
		--panel-depth-4: oklch(0.19 0.014 250);

		/* Accent glow colors */
		--accent-glow: oklch(0.65 0.18 250);
		--accent-glow-dim: oklch(0.50 0.14 250);
		--accent-warm: oklch(0.70 0.16 45);
		--accent-success: oklch(0.68 0.16 145);
		--accent-danger: oklch(0.65 0.20 25);

		/* Geometric line colors */
		--line-bright: oklch(0.55 0.12 250);
		--line-dim: oklch(0.30 0.04 250);
		--line-subtle: oklch(0.22 0.02 250);

		/* Text hierarchy */
		--text-primary: oklch(0.95 0.01 250);
		--text-secondary: oklch(0.72 0.02 250);
		--text-tertiary: oklch(0.55 0.02 250);

		/* Typography */
		--font-display: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
		--font-mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;

		display: flex;
		height: 100%;
		overflow: hidden;
		background:
			/* Noise texture overlay */
			url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.03'/%3E%3C/svg%3E"),
			/* Gradient depth */
			linear-gradient(180deg, var(--panel-depth-1) 0%, var(--panel-depth-2) 100%);
		position: relative;
		font-family: var(--font-display);
	}

	/* Geometric accent line at top */
	.settings-card-content::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 2px;
		background: linear-gradient(
			90deg,
			transparent 0%,
			var(--accent-glow-dim) 15%,
			var(--accent-glow) 50%,
			var(--accent-glow-dim) 85%,
			transparent 100%
		);
		z-index: 10;
	}

	/* Light mode overrides */
	:global(.light) .settings-card-content {
		--panel-depth-1: oklch(0.98 0.004 250);
		--panel-depth-2: oklch(0.96 0.006 250);
		--panel-depth-3: oklch(0.94 0.008 250);
		--panel-depth-4: oklch(0.91 0.010 250);

		--accent-glow: oklch(0.55 0.20 250);
		--accent-glow-dim: oklch(0.45 0.16 250);

		--line-bright: oklch(0.50 0.14 250);
		--line-dim: oklch(0.75 0.04 250);
		--line-subtle: oklch(0.85 0.02 250);

		--text-primary: oklch(0.15 0.01 250);
		--text-secondary: oklch(0.40 0.02 250);
		--text-tertiary: oklch(0.55 0.02 250);

		background:
			url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.02'/%3E%3C/svg%3E"),
			linear-gradient(180deg, var(--panel-depth-1) 0%, var(--panel-depth-2) 100%);
	}

	.settings-card-content.mobile {
		flex-direction: column;
	}

	/* ============================================
	   SIDEBAR - Command Panel Navigation
	   ============================================ */
	.settings-sidebar {
		width: 172px;
		flex-shrink: 0;
		background: var(--panel-depth-1);
		border-right: 1px solid var(--line-dim);
		overflow-y: auto;
		padding: 12px 8px;
		position: relative;
	}

	/* Vertical glowing accent stripe */
	.settings-sidebar::after {
		content: '';
		position: absolute;
		top: 0;
		right: 0;
		width: 1px;
		height: 100%;
		background: linear-gradient(
			180deg,
			transparent 0%,
			var(--accent-glow-dim) 20%,
			var(--accent-glow) 50%,
			var(--accent-glow-dim) 80%,
			transparent 100%
		);
		opacity: 0.6;
	}

	:global(.light) .settings-sidebar {
		background: var(--panel-depth-1);
		border-right-color: var(--line-dim);
	}

	.sidebar-category {
		margin-bottom: 6px;
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 10px 12px;
		border: none;
		background: transparent;
		color: var(--text-secondary);
		font-family: var(--font-display);
		font-size: 0.8125rem;
		font-weight: 600;
		letter-spacing: 0.02em;
		border-radius: 6px;
		cursor: pointer;
		transition: all 0.2s ease;
		text-align: left;
		position: relative;
	}

	/* Glow effect on hover */
	.category-header::before {
		content: '';
		position: absolute;
		inset: 0;
		border-radius: 6px;
		background: radial-gradient(
			ellipse at center,
			var(--accent-glow) 0%,
			transparent 70%
		);
		opacity: 0;
		transition: opacity 0.3s ease;
		pointer-events: none;
	}

	.category-header:hover::before {
		opacity: 0.08;
	}

	:global(.light) .category-header {
		color: var(--text-secondary);
	}

	.category-header:hover {
		background: var(--panel-depth-3);
		color: var(--text-primary);
	}

	:global(.light) .category-header:hover {
		background: var(--panel-depth-3);
		color: var(--text-primary);
	}

	/* Active state with inset glow */
	.category-header.active {
		background: oklch(from var(--accent-glow) l c h / 0.12);
		color: var(--accent-glow);
		box-shadow:
			inset 3px 0 0 var(--accent-glow),
			0 0 20px -8px var(--accent-glow);
	}

	.sidebar-sections {
		margin-left: 16px;
		padding: 4px 0;
		border-left: 1px solid var(--line-subtle);
	}

	.section-btn {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 12px;
		border: none;
		background: transparent;
		color: var(--text-tertiary);
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 500;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		border-radius: 4px;
		cursor: pointer;
		text-align: left;
		transition: all 0.15s ease;
		position: relative;
	}

	:global(.light) .section-btn {
		color: var(--text-tertiary);
	}

	.section-btn:hover {
		background: var(--panel-depth-3);
		color: var(--text-primary);
	}

	:global(.light) .section-btn:hover {
		background: var(--panel-depth-3);
		color: var(--text-primary);
	}

	/* Active indicator dot */
	.section-btn.active {
		color: var(--accent-glow);
		font-weight: 600;
	}

	.section-btn.active::before {
		content: '';
		position: absolute;
		left: -9px;
		top: 50%;
		transform: translateY(-50%);
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--accent-glow);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-glow) l c h / 0.5);
	}

	/* ============================================
	   CONTENT AREA - Main Panel
	   ============================================ */
	.settings-content {
		flex: 1;
		overflow-y: auto;
		padding: 20px 24px;
		scrollbar-width: thin;
		scrollbar-color: var(--line-dim) transparent;
	}

	.settings-content::-webkit-scrollbar {
		width: 8px;
	}

	.settings-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.settings-content::-webkit-scrollbar-thumb {
		background: var(--line-dim);
		border-radius: 4px;
	}

	.settings-content::-webkit-scrollbar-thumb:hover {
		background: var(--line-bright);
	}

	.section-content {
		max-width: 640px;
		animation: fadeSlideIn 0.4s ease-out;
	}

	/* Section Headers with Geometric Underline */
	.section-header {
		margin-bottom: 24px;
		position: relative;
		padding-bottom: 16px;
	}

	.section-header::after {
		content: '';
		position: absolute;
		bottom: 0;
		left: 0;
		width: 60px;
		height: 2px;
		background: linear-gradient(
			90deg,
			var(--accent-glow) 0%,
			transparent 100%
		);
	}

	.section-header h3 {
		font-family: var(--font-display);
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--text-primary);
		letter-spacing: -0.02em;
		margin: 0;
	}

	.section-header p {
		font-family: var(--font-display);
		font-size: 0.875rem;
		color: var(--text-secondary);
		margin-top: 8px;
		line-height: 1.6;
	}

	.section-header .btn {
		margin-top: 12px;
	}

	/* ============================================
	   SETTINGS GROUPS - Layered Cards
	   ============================================ */
	.settings-group {
		background: var(--panel-depth-3);
		border: 1px solid var(--line-dim);
		border-radius: 12px;
		padding: 20px;
		margin-bottom: 20px;
		position: relative;
		overflow: hidden;
		transition: all 0.25s ease;
		animation: fadeSlideIn 0.4s ease-out backwards;
	}

	/* Staggered animation delays */
	.settings-group:nth-child(1) { animation-delay: 0.05s; }
	.settings-group:nth-child(2) { animation-delay: 0.1s; }
	.settings-group:nth-child(3) { animation-delay: 0.15s; }
	.settings-group:nth-child(4) { animation-delay: 0.2s; }
	.settings-group:nth-child(5) { animation-delay: 0.25s; }
	.settings-group:nth-child(6) { animation-delay: 0.3s; }

	/* Geometric corner accent */
	.settings-group::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		width: 40px;
		height: 40px;
		background: linear-gradient(
			135deg,
			oklch(from var(--accent-glow) l c h / 0.15) 0%,
			transparent 50%
		);
		pointer-events: none;
	}

	:global(.light) .settings-group {
		background: var(--panel-depth-3);
		border-color: var(--line-dim);
	}

	.settings-group:hover {
		border-color: var(--line-bright);
		box-shadow:
			0 4px 24px -8px oklch(0 0 0 / 0.3),
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.1);
	}

	:global(.light) .settings-group:hover {
		box-shadow:
			0 4px 24px -8px oklch(0 0 0 / 0.1),
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.15);
	}

	.settings-group h4 {
		font-family: var(--font-display);
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--text-primary);
		margin-bottom: 16px;
		display: flex;
		align-items: center;
		gap: 10px;
		letter-spacing: -0.01em;
	}

	/* ============================================
	   FORM ELEMENTS - Industrial Precision
	   ============================================ */
	.input {
		width: 100%;
		padding: 11px 14px;
		background: var(--panel-depth-2);
		border: 1px solid var(--line-dim);
		border-radius: 8px;
		color: var(--text-primary);
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		transition: all 0.2s ease;
	}

	.input:hover:not(:focus) {
		border-color: var(--line-bright);
	}

	.input:focus {
		outline: none;
		border-color: var(--accent-glow);
		box-shadow:
			0 0 0 3px oklch(from var(--accent-glow) l c h / 0.15),
			0 0 12px -4px var(--accent-glow);
		background: var(--panel-depth-1);
	}

	.input::placeholder {
		color: var(--text-tertiary);
		font-family: var(--font-display);
	}

	.input.small {
		width: 70px;
		text-align: center;
		font-family: var(--font-mono);
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

	:global(.light) select.input {
		background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%235a6478' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.5' d='M6 8l4 4 4-4'/%3e%3c/svg%3e");
	}

	select.input option {
		background-color: var(--panel-depth-2);
		color: var(--text-primary);
		padding: 0.5rem;
	}

	.input-row {
		display: flex;
		gap: 10px;
		align-items: center;
	}

	.form-stack {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}

	.form-stack label {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 500;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--text-secondary);
		margin-bottom: 6px;
		display: block;
	}

	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
	}

	.checkbox-row {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.checkbox-label {
		display: flex;
		align-items: center;
		gap: 10px;
		cursor: pointer;
		font-family: var(--font-display);
		color: var(--text-secondary);
		font-size: 0.875rem;
	}

	.checkbox-label input[type="checkbox"] {
		width: 18px;
		height: 18px;
		cursor: pointer;
		accent-color: var(--accent-glow);
	}

	.info-box {
		background: var(--panel-depth-2);
		border: 1px solid var(--line-dim);
		border-left: 3px solid var(--accent-glow);
		border-radius: 6px;
		padding: 12px 14px;
	}

	:global(.light) .info-box {
		background: var(--panel-depth-2);
		border-color: var(--line-dim);
		border-left-color: var(--accent-glow);
	}

	.badge.info {
		background: oklch(from var(--accent-glow) l c h / 0.2);
		color: var(--accent-glow);
		font-family: var(--font-mono);
		font-size: 0.625rem;
		letter-spacing: 0.03em;
	}

	:global(.light) .badge.info {
		background: oklch(from var(--accent-glow) l c h / 0.15);
	}

	/* ============================================
	   BUTTONS - Industrial Controls
	   ============================================ */
	.btn {
		padding: 11px 20px;
		border: none;
		border-radius: 8px;
		font-family: var(--font-display);
		font-size: 0.8125rem;
		font-weight: 600;
		letter-spacing: 0.01em;
		cursor: pointer;
		transition: all 0.2s ease;
		position: relative;
		overflow: hidden;
	}

	.btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.btn:focus-visible {
		outline: 2px solid var(--accent-glow);
		outline-offset: 2px;
	}

	/* Primary button with gradient and glow */
	.btn-primary {
		background: linear-gradient(
			135deg,
			var(--accent-glow) 0%,
			oklch(from var(--accent-glow) calc(l - 0.08) calc(c + 0.02) h) 100%
		);
		color: oklch(0.98 0 0);
		box-shadow:
			0 2px 8px -2px oklch(from var(--accent-glow) l c h / 0.4),
			inset 0 1px 0 oklch(1 0 0 / 0.15);
	}

	.btn-primary:hover:not(:disabled) {
		transform: translateY(-2px);
		box-shadow:
			0 6px 20px -4px oklch(from var(--accent-glow) l c h / 0.5),
			inset 0 1px 0 oklch(1 0 0 / 0.2);
		filter: brightness(1.1);
	}

	.btn-primary:active:not(:disabled) {
		transform: translateY(0);
		box-shadow:
			0 2px 8px -2px oklch(from var(--accent-glow) l c h / 0.4),
			inset 0 1px 0 oklch(1 0 0 / 0.1);
	}

	/* Secondary button with subtle border */
	.btn-secondary {
		background: var(--panel-depth-2);
		color: var(--text-primary);
		border: 1px solid var(--line-dim);
		box-shadow: 0 1px 2px oklch(0 0 0 / 0.1);
	}

	:global(.light) .btn-secondary {
		background: var(--panel-depth-3);
		color: var(--text-primary);
		border-color: var(--line-dim);
	}

	.btn-secondary:hover:not(:disabled) {
		background: var(--panel-depth-4);
		border-color: var(--line-bright);
		box-shadow: 0 2px 8px oklch(0 0 0 / 0.15);
	}

	:global(.light) .btn-secondary:hover:not(:disabled) {
		background: var(--panel-depth-4);
		border-color: var(--line-bright);
	}

	.btn-sm {
		padding: 7px 14px;
		font-size: 0.75rem;
	}

	.icon-btn {
		padding: 8px;
		background: transparent;
		border: 1px solid transparent;
		cursor: pointer;
		border-radius: 6px;
		transition: all 0.15s ease;
		color: var(--text-secondary);
	}

	.icon-btn:hover {
		background: var(--panel-depth-4);
		border-color: var(--line-dim);
		color: var(--text-primary);
		transform: scale(1.05);
	}

	:global(.light) .icon-btn:hover {
		background: var(--panel-depth-4);
	}

	.icon-btn.danger:hover {
		background: oklch(from var(--accent-danger) l c h / 0.15);
		border-color: oklch(from var(--accent-danger) l c h / 0.3);
		color: var(--accent-danger);
	}

	/* ============================================
	   THEME SELECTOR - Control Panel Switches
	   ============================================ */
	.theme-options {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 12px;
		padding: 4px;
	}

	.theme-btn {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 10px;
		padding: 16px 14px;
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 2px solid var(--line-dim);
		border-radius: 12px;
		cursor: pointer;
		transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
		color: var(--text-primary);
		font-family: var(--font-display);
		font-weight: 500;
		position: relative;
		overflow: hidden;
		/* Industrial inset effect */
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.05),
			inset 0 -2px 4px oklch(0 0 0 / 0.15),
			0 2px 4px oklch(0 0 0 / 0.1);
	}

	/* Scanline overlay for industrial feel */
	.theme-btn::before {
		content: '';
		position: absolute;
		inset: 0;
		background: repeating-linear-gradient(
			0deg,
			transparent 0px,
			transparent 2px,
			oklch(0 0 0 / 0.03) 2px,
			oklch(0 0 0 / 0.03) 4px
		);
		pointer-events: none;
		opacity: 0.5;
	}

	/* Status indicator light */
	.theme-btn::after {
		content: '';
		position: absolute;
		top: 8px;
		right: 8px;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--text-tertiary);
		box-shadow: 0 0 4px 1px oklch(from var(--text-tertiary) l c h / 0.3);
		transition: all 0.3s ease;
	}

	:global(.light) .theme-btn {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
		color: var(--text-primary);
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.5),
			inset 0 -2px 4px oklch(0 0 0 / 0.05),
			0 2px 4px oklch(0 0 0 / 0.05);
	}

	.theme-btn:hover {
		border-color: oklch(from var(--accent-glow) l c h / 0.6);
		transform: translateY(-3px);
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.08),
			inset 0 -2px 4px oklch(0 0 0 / 0.1),
			0 8px 24px oklch(0 0 0 / 0.25),
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.15);
	}

	.theme-btn:hover::after {
		background: var(--accent-glow-dim);
		box-shadow: 0 0 6px 2px oklch(from var(--accent-glow) l c h / 0.4);
	}

	.theme-btn.active {
		border-color: var(--accent-glow);
		background:
			linear-gradient(180deg, oklch(from var(--accent-glow) l c h / 0.15) 0%, oklch(from var(--accent-glow) l c h / 0.08) 100%);
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.1),
			inset 0 -2px 4px oklch(0 0 0 / 0.1),
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.4),
			0 0 20px -4px oklch(from var(--accent-glow) l c h / 0.4),
			0 6px 20px oklch(0 0 0 / 0.2);
		animation: subtleGlow 3s ease-in-out infinite;
	}

	.theme-btn.active::after {
		background: var(--accent-glow);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-glow) l c h / 0.6);
		animation: statusPulse 2s ease-in-out infinite;
	}

	.theme-preview {
		width: 44px;
		height: 44px;
		border-radius: 10px;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.375rem;
		position: relative;
		/* Industrial bezel effect */
		box-shadow:
			0 2px 8px rgba(0,0,0,0.25),
			inset 0 1px 0 oklch(1 0 0 / 0.15),
			inset 0 -1px 0 oklch(0 0 0 / 0.2);
	}

	.theme-preview.light {
		background: linear-gradient(180deg, oklch(0.99 0 0) 0%, oklch(0.94 0 0) 100%);
		border: 1px solid oklch(0.85 0.01 250);
	}

	.theme-preview.dark {
		background: linear-gradient(180deg, oklch(0.16 0.015 250) 0%, oklch(0.10 0.015 250) 100%);
		border: 1px solid oklch(0.30 0.02 250);
	}

	.theme-preview.system {
		background: linear-gradient(135deg, oklch(0.98 0 0) 50%, oklch(0.12 0.015 250) 50%);
		border: 1px solid oklch(0.50 0.01 250);
	}

	/* Theme label styling */
	.theme-btn span:not(.theme-preview) {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-secondary);
		transition: color 0.25s ease;
	}

	.theme-btn.active span:not(.theme-preview) {
		color: var(--accent-glow);
	}

	/* ============================================
	   TOGGLES - Refined Switches
	   ============================================ */
	.toggle {
		width: 46px;
		height: 26px;
		background: var(--panel-depth-2);
		border: 1px solid var(--line-dim);
		border-radius: 13px;
		cursor: pointer;
		position: relative;
		transition: all 0.25s ease;
		box-shadow: inset 0 1px 3px oklch(0 0 0 / 0.15);
	}

	:global(.light) .toggle {
		background: var(--panel-depth-4);
		border-color: var(--line-dim);
	}

	.toggle:hover {
		border-color: var(--line-bright);
	}

	:global(.light) .toggle:hover {
		border-color: var(--line-bright);
	}

	.toggle.active {
		background: var(--accent-glow);
		border-color: var(--accent-glow);
		box-shadow:
			inset 0 1px 2px oklch(0 0 0 / 0.1),
			0 0 12px -2px oklch(from var(--accent-glow) l c h / 0.4);
	}

	.toggle-knob {
		position: absolute;
		top: 3px;
		left: 3px;
		width: 18px;
		height: 18px;
		background: linear-gradient(180deg, oklch(0.98 0 0) 0%, oklch(0.92 0 0) 100%);
		border-radius: 50%;
		transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
		box-shadow: 0 1px 4px oklch(0 0 0 / 0.25);
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}

	.toggle-sm {
		width: 38px;
		height: 22px;
		background: var(--panel-depth-2);
		border: 1px solid var(--line-dim);
		border-radius: 11px;
		cursor: pointer;
		position: relative;
		transition: all 0.25s ease;
		box-shadow: inset 0 1px 2px oklch(0 0 0 / 0.15);
	}

	:global(.light) .toggle-sm {
		background: var(--panel-depth-4);
		border-color: var(--line-dim);
	}

	.toggle-sm.active {
		background: var(--accent-glow);
		border-color: var(--accent-glow);
		box-shadow:
			inset 0 1px 2px oklch(0 0 0 / 0.1),
			0 0 10px -2px oklch(from var(--accent-glow) l c h / 0.4);
	}

	.toggle-knob-sm {
		position: absolute;
		top: 3px;
		left: 3px;
		width: 14px;
		height: 14px;
		background: linear-gradient(180deg, oklch(0.98 0 0) 0%, oklch(0.92 0 0) 100%);
		border-radius: 50%;
		transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
		box-shadow: 0 1px 3px oklch(0 0 0 / 0.25);
	}

	.toggle-sm.active .toggle-knob-sm {
		transform: translateX(16px);
	}

	.toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 14px;
	}

	/* ============================================
	   BADGES - Status Indicators
	   ============================================ */
	.badge {
		font-family: var(--font-mono);
		font-size: 0.625rem;
		font-weight: 500;
		padding: 3px 8px;
		border-radius: 4px;
		letter-spacing: 0.02em;
		text-transform: uppercase;
	}

	.badge.success {
		background: oklch(from var(--accent-success) l c h / 0.15);
		color: var(--accent-success);
	}

	.badge.error {
		background: oklch(from var(--accent-danger) l c h / 0.15);
		color: var(--accent-danger);
	}

	.badge.muted {
		background: var(--panel-depth-2);
		color: var(--text-tertiary);
	}

	.badge.primary {
		background: oklch(from var(--accent-glow) l c h / 0.15);
		color: var(--accent-glow);
	}

	/* ============================================
	   MESSAGES - Banners (Alert Status Display)
	   ============================================ */
	.error-banner {
		background:
			linear-gradient(135deg,
				oklch(from var(--accent-danger) l c h / 0.15) 0%,
				oklch(from var(--accent-danger) l c h / 0.08) 100%);
		border: 1px solid oklch(from var(--accent-danger) l c h / 0.35);
		box-shadow:
			0 0 12px oklch(from var(--accent-danger) l c h / 0.2),
			inset 0 1px 0 oklch(from var(--accent-danger) l c h / 0.15);
		color: var(--accent-danger);
		padding: 14px 18px;
		border-radius: 8px;
		font-family: var(--font-display);
		font-size: 0.875rem;
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 16px;
		position: relative;
		overflow: hidden;
		animation: bannerGlow 2s ease-in-out infinite;
		--glow-color: var(--accent-danger);
	}

	/* Scanline overlay for error banner */
	.error-banner::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		bottom: 0;
		background: repeating-linear-gradient(
			0deg,
			transparent,
			transparent 2px,
			oklch(from var(--accent-danger) l c h / 0.03) 2px,
			oklch(from var(--accent-danger) l c h / 0.03) 4px
		);
		pointer-events: none;
	}

	/* Alert indicator for error banner */
	.error-banner::after {
		content: '';
		position: absolute;
		top: 50%;
		left: 8px;
		width: 6px;
		height: 6px;
		background: var(--accent-danger);
		border-radius: 50%;
		transform: translateY(-50%);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-danger) l c h / 0.6);
		animation: statusPulse 1s ease-in-out infinite;
	}

	:global(.light) .error-banner {
		background:
			linear-gradient(135deg,
				oklch(from var(--accent-danger) l c h / 0.12) 0%,
				oklch(from var(--accent-danger) l c h / 0.06) 100%);
		box-shadow:
			0 0 8px oklch(from var(--accent-danger) l c h / 0.15),
			inset 0 1px 0 oklch(1 0 0 / 0.5);
	}

	.success-banner {
		background:
			linear-gradient(135deg,
				oklch(from var(--accent-success) l c h / 0.15) 0%,
				oklch(from var(--accent-success) l c h / 0.08) 100%);
		border: 1px solid oklch(from var(--accent-success) l c h / 0.35);
		box-shadow:
			0 0 12px oklch(from var(--accent-success) l c h / 0.2),
			inset 0 1px 0 oklch(from var(--accent-success) l c h / 0.15);
		color: var(--accent-success);
		padding: 14px 18px 14px 28px;
		border-radius: 8px;
		font-family: var(--font-display);
		font-size: 0.875rem;
		margin-bottom: 16px;
		position: relative;
		overflow: hidden;
		animation: bannerGlow 2.5s ease-in-out infinite;
		--glow-color: var(--accent-success);
	}

	/* Scanline overlay for success banner */
	.success-banner::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		bottom: 0;
		background: repeating-linear-gradient(
			0deg,
			transparent,
			transparent 2px,
			oklch(from var(--accent-success) l c h / 0.03) 2px,
			oklch(from var(--accent-success) l c h / 0.03) 4px
		);
		pointer-events: none;
	}

	/* Status indicator for success banner */
	.success-banner::after {
		content: '';
		position: absolute;
		top: 50%;
		left: 8px;
		width: 6px;
		height: 6px;
		background: var(--accent-success);
		border-radius: 50%;
		transform: translateY(-50%);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-success) l c h / 0.6);
		animation: statusPulse 1.5s ease-in-out infinite;
	}

	:global(.light) .success-banner {
		background:
			linear-gradient(135deg,
				oklch(from var(--accent-success) l c h / 0.12) 0%,
				oklch(from var(--accent-success) l c h / 0.06) 100%);
		box-shadow:
			0 0 8px oklch(from var(--accent-success) l c h / 0.15),
			inset 0 1px 0 oklch(1 0 0 / 0.5);
	}

	.close-btn {
		background: oklch(from var(--panel-depth-1) l c h / 0.5);
		border: 1px solid oklch(from var(--line-dim) l c h / 0.3);
		border-radius: 4px;
		font-size: 1.25rem;
		cursor: pointer;
		opacity: 0.7;
		padding: 2px 8px;
		transition: all 0.15s ease;
		position: relative;
		z-index: 1;
	}

	.close-btn:hover {
		opacity: 1;
		background: oklch(from var(--panel-depth-2) l c h / 0.8);
		transform: scale(1.05);
	}

	:global(.light) .close-btn {
		background: oklch(1 0 0 / 0.5);
		border-color: oklch(from var(--line-dim) l c h / 0.4);
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
	   CONNECTION CARDS - Data Terminal Style
	   ============================================ */
	.connection-card {
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 1px solid var(--line-dim);
		border-radius: 12px;
		padding: 18px;
		margin-bottom: 14px;
		transition: all 0.25s ease;
		position: relative;
		overflow: hidden;
	}

	/* Left accent bar */
	.connection-card::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		width: 3px;
		height: 100%;
		background: linear-gradient(
			180deg,
			var(--accent-glow) 0%,
			var(--accent-glow-dim) 50%,
			transparent 100%
		);
		opacity: 0;
		transition: opacity 0.25s ease;
	}

	/* Scanline overlay for terminal feel */
	.connection-card::after {
		content: '';
		position: absolute;
		inset: 0;
		background: repeating-linear-gradient(
			0deg,
			transparent 0px,
			transparent 3px,
			oklch(0 0 0 / 0.02) 3px,
			oklch(0 0 0 / 0.02) 6px
		);
		pointer-events: none;
		opacity: 0.4;
	}

	:global(.light) .connection-card {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
	}

	.connection-card:hover {
		border-color: var(--line-bright);
		box-shadow:
			0 4px 20px oklch(0 0 0 / 0.15),
			inset 0 0 40px -20px oklch(from var(--accent-glow) l c h / 0.1);
	}

	.connection-card:hover::before {
		opacity: 1;
	}

	/* Connected state with animated indicator */
	.connection-card.connected {
		border-color: oklch(from var(--accent-success) l c h / 0.4);
	}

	.connection-card.connected::before {
		opacity: 1;
		background: linear-gradient(
			180deg,
			var(--accent-success) 0%,
			oklch(from var(--accent-success) l c h / 0.5) 50%,
			transparent 100%
		);
	}

	.connection-header {
		display: flex;
		align-items: center;
		gap: 14px;
		margin-bottom: 14px;
		position: relative;
		z-index: 1;
	}

	.connection-icon {
		width: 48px;
		height: 48px;
		border-radius: 10px;
		background:
			linear-gradient(135deg, var(--panel-depth-2) 0%, var(--panel-depth-1) 100%);
		border: 1px solid var(--line-subtle);
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.375rem;
		position: relative;
		/* Industrial bezel */
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.05),
			inset 0 -1px 3px oklch(0 0 0 / 0.2),
			0 2px 4px oklch(0 0 0 / 0.1);
	}

	/* Status light on icon */
	.connection-icon::after {
		content: '';
		position: absolute;
		top: -2px;
		right: -2px;
		width: 10px;
		height: 10px;
		border-radius: 50%;
		background: var(--text-tertiary);
		border: 2px solid var(--panel-depth-3);
		box-shadow: 0 0 4px 1px oklch(from var(--text-tertiary) l c h / 0.3);
		transition: all 0.3s ease;
	}

	.connection-card.connected .connection-icon::after {
		background: var(--accent-success);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-success) l c h / 0.5);
		animation: connectionPulse 2s ease-in-out infinite;
	}

	:global(.light) .connection-icon {
		background:
			linear-gradient(135deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
	}

	/* Service-specific icon colors */
	.connection-icon.github {
		background: linear-gradient(135deg, oklch(0.20 0.01 250) 0%, oklch(0.12 0.01 250) 100%);
		border-color: oklch(0.35 0.02 250);
		color: oklch(0.95 0 0);
	}

	.connection-icon.claude {
		background: linear-gradient(135deg, oklch(0.22 0.08 25) 0%, oklch(0.15 0.06 25) 100%);
		border-color: oklch(0.40 0.12 25);
	}

	.connection-icon.orange {
		background: linear-gradient(135deg, oklch(from var(--accent-warm) l c h / 0.20) 0%, oklch(from var(--accent-warm) l c h / 0.12) 100%);
		border-color: oklch(from var(--accent-warm) l c h / 0.35);
	}

	.connection-icon.docker {
		background: linear-gradient(135deg, oklch(0.50 0.15 230) 0%, oklch(0.38 0.12 230) 100%);
		border-color: oklch(0.55 0.16 230);
	}

	.connection-title {
		display: flex;
		align-items: center;
		gap: 10px;
		font-family: var(--font-display);
		font-weight: 600;
		color: var(--text-primary);
	}

	:global(.light) .connection-title {
		color: var(--text-primary);
	}

	.connection-info p {
		color: var(--text-secondary);
		font-family: var(--font-display);
		font-size: 0.875rem;
	}

	:global(.light) .connection-info p {
		color: var(--text-secondary);
	}

	.connection-actions {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.oauth-flow {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	/* Docker info */
	.docker-info {
		display: flex;
		align-items: flex-start;
		gap: 14px;
		padding: 14px;
		background: oklch(from var(--accent-glow) l c h / 0.08);
		border: 1px solid oklch(from var(--accent-glow) l c h / 0.2);
		border-radius: 10px;
	}

	.docker-info p {
		color: var(--text-primary);
		font-family: var(--font-display);
	}

	:global(.light) .docker-info p {
		color: var(--text-primary);
	}

	.docker-info code {
		background: var(--panel-depth-1);
		padding: 3px 8px;
		border-radius: 4px;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--accent-glow);
	}

	:global(.light) .docker-info code {
		background: var(--panel-depth-4);
		color: var(--accent-glow);
	}

	/* Info card */
	.info-card {
		display: flex;
		align-items: center;
		gap: 14px;
		padding: 14px;
		border-radius: 10px;
	}

	.info-card.success {
		background: oklch(from var(--accent-success) l c h / 0.1);
		border: 1px solid oklch(from var(--accent-success) l c h / 0.25);
	}

	.info-card .icon {
		font-size: 1.5rem;
	}

	.info-card h4 {
		font-family: var(--font-display);
		font-size: 0.9375rem;
		font-weight: 600;
		margin: 0;
		color: var(--text-primary);
	}

	:global(.light) .info-card h4 {
		color: var(--text-primary);
	}

	.info-card p {
		font-family: var(--font-display);
		font-size: 0.8125rem;
		color: var(--text-secondary);
		margin: 3px 0 0;
	}

	:global(.light) .info-card p {
		color: var(--text-secondary);
	}

	/* ============================================
	   USER LIST - Personnel Registry Style
	   ============================================ */
	.user-list {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.user-card {
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 1px solid var(--line-dim);
		border-radius: 12px;
		padding: 16px;
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 14px;
		transition: all 0.25s ease;
		position: relative;
		overflow: hidden;
	}

	/* Left accent stripe */
	.user-card::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		width: 3px;
		height: 100%;
		background: var(--line-dim);
		transition: all 0.25s ease;
	}

	/* Status indicator area */
	.user-card::after {
		content: '';
		position: absolute;
		top: 12px;
		left: 10px;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--text-tertiary);
		box-shadow: 0 0 4px 1px oklch(from var(--text-tertiary) l c h / 0.3);
		transition: all 0.3s ease;
	}

	:global(.light) .user-card {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
	}

	.user-card:hover {
		border-color: var(--line-bright);
		box-shadow:
			0 4px 16px oklch(0 0 0 / 0.15),
			inset 0 0 30px -15px oklch(from var(--accent-glow) l c h / 0.08);
	}

	.user-card:hover::before {
		background: linear-gradient(180deg, var(--accent-glow) 0%, var(--accent-glow-dim) 100%);
	}

	/* Active user status */
	.user-card.active::before {
		background: linear-gradient(180deg, var(--accent-success) 0%, oklch(from var(--accent-success) l c h / 0.5) 100%);
	}

	.user-card.active::after {
		background: var(--accent-success);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-success) l c h / 0.5);
		animation: statusPulse 2s ease-in-out infinite;
	}

	/* Inactive/disabled user */
	.user-card.inactive::after {
		background: var(--accent-danger);
		box-shadow: 0 0 6px 1px oklch(from var(--accent-danger) l c h / 0.4);
	}

	.user-info {
		flex: 1;
		min-width: 0;
		padding-left: 20px;
	}

	.user-header {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
	}

	.user-name {
		font-family: var(--font-display);
		font-weight: 700;
		font-size: 0.9375rem;
		color: var(--text-primary);
	}

	:global(.light) .user-name {
		color: var(--text-primary);
	}

	.user-desc {
		font-family: var(--font-display);
		font-size: 0.8125rem;
		color: var(--text-secondary);
		margin: 6px 0;
		line-height: 1.45;
	}

	:global(.light) .user-desc {
		color: var(--text-secondary);
	}

	.user-meta {
		display: flex;
		gap: 18px;
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--text-tertiary);
		letter-spacing: 0.03em;
		text-transform: uppercase;
	}

	.user-meta span {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	:global(.light) .user-meta {
		color: var(--text-tertiary);
	}

	.user-actions {
		display: flex;
		gap: 8px;
		position: relative;
		z-index: 1;
	}

	.user-actions button {
		background: var(--panel-depth-2);
		border: 1px solid var(--line-dim);
		border-radius: 8px;
		padding: 8px 12px;
		font-family: var(--font-display);
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.2s ease;
	}

	.user-actions button:hover {
		background: var(--panel-depth-4);
		border-color: var(--line-bright);
		color: var(--text-primary);
		transform: translateY(-1px);
		box-shadow: 0 3px 8px oklch(0 0 0 / 0.15);
	}

	.user-actions button.danger:hover {
		background: oklch(from var(--accent-danger) l c h / 0.15);
		border-color: oklch(from var(--accent-danger) l c h / 0.4);
		color: var(--accent-danger);
	}

	.loading, .empty-state {
		text-align: center;
		padding: 40px;
		color: var(--text-secondary);
		font-family: var(--font-display);
	}

	:global(.light) .loading,
	:global(.light) .empty-state {
		color: var(--text-secondary);
	}

	/* ============================================
	   API KEY CARDS - Secure Terminal Style
	   ============================================ */
	.api-key-card {
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 1px solid var(--line-dim);
		border-radius: 12px;
		padding: 18px;
		margin-bottom: 14px;
		transition: all 0.25s ease;
		position: relative;
		overflow: hidden;
	}

	/* Left accent stripe - provider color */
	.api-key-card::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		width: 4px;
		height: 100%;
		background: var(--line-dim);
		transition: all 0.3s ease;
	}

	/* Secured indicator corner */
	.api-key-card::after {
		content: '';
		position: absolute;
		top: 0;
		right: 0;
		width: 0;
		height: 0;
		border-style: solid;
		border-width: 0 24px 24px 0;
		border-color: transparent var(--panel-depth-2) transparent transparent;
		transition: border-color 0.3s ease;
	}

	.api-key-card.configured::after {
		border-color: transparent oklch(from var(--accent-success) l c h / 0.3) transparent transparent;
	}

	:global(.light) .api-key-card {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
	}

	.api-key-card:hover {
		border-color: var(--line-bright);
		box-shadow:
			0 4px 20px oklch(0 0 0 / 0.15),
			inset 0 0 30px -15px oklch(from var(--accent-glow) l c h / 0.08);
	}

	/* Provider-specific accent colors */
	.api-key-card.openai::before {
		background: linear-gradient(180deg, oklch(0.70 0.18 145) 0%, oklch(0.50 0.14 145) 100%);
	}

	.api-key-card.openai:hover {
		border-color: oklch(from var(--accent-success) l c h / 0.4);
		box-shadow:
			0 4px 20px oklch(0 0 0 / 0.15),
			0 0 30px -10px oklch(from var(--accent-success) l c h / 0.2);
	}

	.api-key-card.google::before {
		background: linear-gradient(180deg, oklch(0.65 0.18 250) 0%, oklch(0.45 0.14 250) 100%);
	}

	.api-key-card.google:hover {
		border-color: oklch(from var(--accent-glow) l c h / 0.4);
		box-shadow:
			0 4px 20px oklch(0 0 0 / 0.15),
			0 0 30px -10px oklch(from var(--accent-glow) l c h / 0.2);
	}

	.api-key-card.meshy::before {
		background: linear-gradient(180deg, oklch(0.60 0.20 300) 0%, oklch(0.40 0.16 300) 100%);
	}

	.api-key-card.meshy:hover {
		border-color: oklch(0.55 0.18 300 / 0.4);
		box-shadow:
			0 4px 20px oklch(0 0 0 / 0.15),
			0 0 30px -10px oklch(0.55 0.18 300 / 0.2);
	}

	.api-key-card.elevenlabs::before {
		background: linear-gradient(180deg, oklch(0.65 0.15 45) 0%, oklch(0.45 0.12 45) 100%);
	}

	.api-key-card.anthropic::before {
		background: linear-gradient(180deg, oklch(0.60 0.16 25) 0%, oklch(0.40 0.12 25) 100%);
	}

	.api-key-header {
		display: flex;
		align-items: center;
		gap: 14px;
		margin-bottom: 14px;
		position: relative;
		z-index: 1;
	}

	.api-icon {
		width: 48px;
		height: 48px;
		border-radius: 10px;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 1.375rem;
		border: 1px solid transparent;
		position: relative;
		/* Industrial bezel */
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.08),
			inset 0 -2px 4px oklch(0 0 0 / 0.15),
			0 2px 4px oklch(0 0 0 / 0.1);
	}

	/* Secured lock indicator on icon */
	.api-icon.secured::after {
		content: '';
		position: absolute;
		bottom: -2px;
		right: -2px;
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--accent-success);
		border: 2px solid var(--panel-depth-3);
		box-shadow: 0 0 6px 2px oklch(from var(--accent-success) l c h / 0.4);
	}

	.api-icon.openai {
		background: linear-gradient(135deg, oklch(from var(--accent-success) l c h / 0.18) 0%, oklch(from var(--accent-success) l c h / 0.10) 100%);
		border-color: oklch(from var(--accent-success) l c h / 0.30);
	}

	.api-icon.google {
		background: linear-gradient(135deg, oklch(from var(--accent-glow) l c h / 0.18) 0%, oklch(from var(--accent-glow) l c h / 0.10) 100%);
		border-color: oklch(from var(--accent-glow) l c h / 0.30);
	}

	.api-icon.meshy {
		background: linear-gradient(135deg, oklch(0.55 0.18 300 / 0.18) 0%, oklch(0.55 0.18 300 / 0.10) 100%);
		border-color: oklch(0.55 0.18 300 / 0.30);
	}

	.api-icon.elevenlabs {
		background: linear-gradient(135deg, oklch(0.60 0.14 45 / 0.18) 0%, oklch(0.60 0.14 45 / 0.10) 100%);
		border-color: oklch(0.60 0.14 45 / 0.30);
	}

	.api-icon.anthropic {
		background: linear-gradient(135deg, oklch(0.55 0.14 25 / 0.18) 0%, oklch(0.55 0.14 25 / 0.10) 100%);
		border-color: oklch(0.55 0.14 25 / 0.30);
	}

	.api-title {
		display: flex;
		align-items: center;
		gap: 10px;
		font-family: var(--font-display);
		font-weight: 600;
		color: var(--text-primary);
	}

	:global(.light) .api-title {
		color: var(--text-primary);
	}

	.api-key-form {
		display: flex;
		flex-direction: column;
		gap: 10px;
		position: relative;
		z-index: 1;
	}

	/* Masked key display styling */
	.api-key-masked {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		letter-spacing: 0.1em;
		color: var(--text-tertiary);
		background: var(--panel-depth-1);
		padding: 8px 12px;
		border-radius: 6px;
		border: 1px solid var(--line-subtle);
	}

	/* ============================================
	   MODEL GRID / CARDS
	   ============================================ */
	.model-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
		gap: 10px;
	}

	.model-btn {
		display: flex;
		flex-direction: column;
		padding: 12px;
		background: var(--panel-depth-2);
		border: 2px solid var(--line-dim);
		border-radius: 10px;
		cursor: pointer;
		transition: all 0.2s ease;
		text-align: left;
		font-family: var(--font-display);
	}

	.model-btn:hover:not(:disabled) {
		border-color: oklch(from var(--accent-glow) l c h / 0.5);
		background: var(--panel-depth-3);
	}

	.model-btn.active {
		border-color: var(--accent-glow);
		background: oklch(from var(--accent-glow) l c h / 0.1);
		box-shadow: 0 0 0 1px oklch(from var(--accent-glow) l c h / 0.2);
	}

	.model-btn.disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.model-name {
		font-family: var(--font-display);
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.model-price {
		font-family: var(--font-mono);
		font-size: 0.625rem;
		color: var(--accent-glow);
		margin-top: auto;
		padding-top: 6px;
	}

	.model-provider {
		font-family: var(--font-mono);
		font-size: 0.625rem;
		color: var(--text-tertiary);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	/* Audio models */
	.audio-models {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
	}

	.audio-model-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.audio-btn {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 10px 14px;
		background: var(--panel-depth-2);
		border: 2px solid var(--line-dim);
		border-radius: 8px;
		cursor: pointer;
		font-family: var(--font-display);
		font-size: 0.8125rem;
		transition: all 0.2s ease;
	}

	.audio-btn:hover:not(:disabled) {
		border-color: oklch(from var(--accent-glow) l c h / 0.5);
	}

	.audio-btn.active {
		border-color: var(--accent-glow);
		background: oklch(from var(--accent-glow) l c h / 0.1);
	}

	.audio-btn .price {
		font-family: var(--font-mono);
		color: var(--accent-glow);
		font-size: 0.75rem;
	}

	/* ============================================
	   CLEANUP SECTION - System Maintenance Panel
	   ============================================ */
	.cleanup-status {
		display: flex;
		align-items: center;
		gap: 16px;
		padding: 16px 20px;
		border-radius: 12px;
		margin-bottom: 18px;
		position: relative;
		overflow: hidden;
	}

	/* Scanline overlay */
	.cleanup-status::before {
		content: '';
		position: absolute;
		inset: 0;
		background: repeating-linear-gradient(
			0deg,
			transparent 0px,
			transparent 2px,
			oklch(0 0 0 / 0.02) 2px,
			oklch(0 0 0 / 0.02) 4px
		);
		pointer-events: none;
		opacity: 0.5;
	}

	/* Status indicator pulse bar */
	.cleanup-status::after {
		content: '';
		position: absolute;
		left: 0;
		top: 0;
		width: 4px;
		height: 100%;
		border-radius: 4px 0 0 4px;
	}

	.cleanup-status.active {
		background:
			linear-gradient(135deg, oklch(from var(--accent-success) l c h / 0.12) 0%, oklch(from var(--accent-success) l c h / 0.06) 100%);
		border: 1px solid oklch(from var(--accent-success) l c h / 0.30);
		box-shadow:
			0 0 20px -5px oklch(from var(--accent-success) l c h / 0.15),
			inset 0 0 30px -15px oklch(from var(--accent-success) l c h / 0.1);
	}

	.cleanup-status.active::after {
		background: var(--accent-success);
		box-shadow: 0 0 10px 2px oklch(from var(--accent-success) l c h / 0.4);
		animation: dataFlow 3s linear infinite;
	}

	.cleanup-status.sleeping {
		background:
			linear-gradient(135deg, oklch(from var(--accent-warm) l c h / 0.12) 0%, oklch(from var(--accent-warm) l c h / 0.06) 100%);
		border: 1px solid oklch(from var(--accent-warm) l c h / 0.30);
	}

	.cleanup-status.sleeping::after {
		background: var(--accent-warm);
		box-shadow: 0 0 8px 1px oklch(from var(--accent-warm) l c h / 0.3);
		animation: statusPulse 3s ease-in-out infinite;
	}

	.cleanup-status p {
		color: var(--text-primary);
		font-family: var(--font-display);
		font-weight: 500;
		position: relative;
		z-index: 1;
	}

	:global(.light) .cleanup-status p {
		color: var(--text-primary);
	}

	.status-icon {
		font-size: 1.75rem;
		position: relative;
		z-index: 1;
	}

	.cleanup-status.active .status-icon {
		animation: connectionPulse 2s ease-in-out infinite;
	}

	.cleanup-inputs {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
		margin-top: 14px;
	}

	.cleanup-inputs.three-col {
		grid-template-columns: repeat(3, 1fr);
	}

	.cleanup-inputs label {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--text-secondary);
		display: block;
		margin-bottom: 6px;
	}

	:global(.light) .cleanup-inputs label {
		color: var(--text-secondary);
	}

	.cleanup-items {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.cleanup-item {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 14px 16px;
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 1px solid var(--line-dim);
		border-radius: 10px;
		color: var(--text-primary);
		font-family: var(--font-display);
		font-weight: 500;
		transition: all 0.25s ease;
		position: relative;
	}

	.cleanup-item::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0;
		width: 3px;
		height: 100%;
		background: var(--line-dim);
		border-radius: 3px 0 0 3px;
		transition: all 0.25s ease;
	}

	.cleanup-item:hover {
		border-color: var(--line-bright);
		box-shadow: 0 2px 12px oklch(0 0 0 / 0.1);
	}

	.cleanup-item:hover::before {
		background: var(--accent-glow);
	}

	:global(.light) .cleanup-item {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
		color: var(--text-primary);
	}

	.cleanup-controls {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	/* Industrial switch toggle for cleanup */
	.cleanup-controls .toggle-sm {
		background:
			linear-gradient(180deg, var(--panel-depth-2) 0%, var(--panel-depth-1) 100%);
		border: 1px solid var(--line-dim);
		box-shadow:
			inset 0 1px 3px oklch(0 0 0 / 0.15),
			0 1px 0 oklch(1 0 0 / 0.03);
	}

	.cleanup-controls .toggle-sm.active {
		background: linear-gradient(180deg, var(--accent-glow) 0%, oklch(from var(--accent-glow) calc(l - 0.1) c h) 100%);
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.2),
			0 0 12px -2px oklch(from var(--accent-glow) l c h / 0.5);
	}

	.cleanup-actions {
		display: flex;
		gap: 14px;
		margin-top: 18px;
	}

	/* Key display - Secure Terminal Style */
	.key-display {
		background:
			linear-gradient(135deg, oklch(from var(--accent-success) l c h / 0.12) 0%, oklch(from var(--accent-success) l c h / 0.06) 100%);
		border: 1px solid oklch(from var(--accent-success) l c h / 0.30);
		border-radius: 12px;
		padding: 16px;
		margin-top: 14px;
		position: relative;
		overflow: hidden;
	}

	/* Scanline effect */
	.key-display::before {
		content: '';
		position: absolute;
		inset: 0;
		background: repeating-linear-gradient(
			0deg,
			transparent 0px,
			transparent 2px,
			oklch(0 0 0 / 0.02) 2px,
			oklch(0 0 0 / 0.02) 4px
		);
		pointer-events: none;
		animation: scanline 0.1s linear infinite;
		opacity: 0.3;
	}

	/* Corner accent */
	.key-display::after {
		content: 'SECURED';
		position: absolute;
		top: 8px;
		right: 12px;
		font-family: var(--font-mono);
		font-size: 0.5625rem;
		font-weight: 700;
		letter-spacing: 0.1em;
		color: var(--accent-success);
		opacity: 0.7;
	}

	.key-display p {
		color: var(--text-primary);
		font-family: var(--font-display);
		font-weight: 500;
		position: relative;
		z-index: 1;
	}

	:global(.light) .key-display p {
		color: var(--text-primary);
	}

	.key-display code {
		background: var(--panel-depth-1);
		padding: 12px 16px;
		border-radius: 8px;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		word-break: break-all;
		color: var(--accent-success);
		display: block;
		margin-top: 8px;
	}

	:global(.light) .key-display code {
		background: var(--panel-depth-4);
		color: var(--accent-success);
	}

	/* ============================================
	   MODAL
	   ============================================ */
	.modal-overlay {
		position: fixed;
		inset: 0;
		background: oklch(0.05 0.01 250 / 0.85);
		backdrop-filter: blur(8px);
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 20px;
		z-index: 100;
	}

	.modal-content {
		background: var(--panel-depth-2);
		border: 1px solid var(--line-dim);
		border-radius: 16px;
		width: 100%;
		max-width: 480px;
		max-height: 90vh;
		overflow-y: auto;
		box-shadow:
			0 20px 60px oklch(0 0 0 / 0.4),
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.1);
	}

	:global(.light) .modal-content {
		background: oklch(0.99 0 0);
		border-color: var(--line-dim);
		box-shadow:
			0 20px 60px oklch(0 0 0 / 0.2),
			0 0 0 1px var(--line-dim);
	}

	.modal-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 18px 20px;
		border-bottom: 1px solid var(--line-dim);
		position: relative;
	}

	.modal-header::after {
		content: '';
		position: absolute;
		bottom: -1px;
		left: 20px;
		width: 50px;
		height: 2px;
		background: var(--accent-glow);
	}

	:global(.light) .modal-header {
		border-bottom-color: var(--line-dim);
	}

	.modal-header h3 {
		font-family: var(--font-display);
		font-size: 1.0625rem;
		font-weight: 700;
		color: var(--text-primary);
	}

	:global(.light) .modal-header h3 {
		color: var(--text-primary);
	}

	.modal-body {
		padding: 20px;
	}

	.modal-body label {
		color: var(--text-primary);
		font-family: var(--font-display);
	}

	:global(.light) .modal-body label {
		color: var(--text-primary);
	}

	/* ============================================
	   MOBILE STYLES
	   ============================================ */
	.mobile-header {
		padding: 14px;
		border-bottom: 1px solid var(--line-dim);
		background: var(--panel-depth-1);
		position: relative;
	}

	.mobile-header::after {
		content: '';
		position: absolute;
		bottom: 0;
		left: 0;
		right: 0;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent 0%,
			var(--accent-glow-dim) 50%,
			transparent 100%
		);
	}

	:global(.light) .mobile-header {
		background: var(--panel-depth-1);
		border-bottom-color: var(--line-dim);
	}

	.menu-toggle {
		display: flex;
		align-items: center;
		gap: 10px;
		background: none;
		border: none;
		color: var(--text-primary);
		font-family: var(--font-display);
		font-size: 0.9375rem;
		font-weight: 600;
		cursor: pointer;
	}

	:global(.light) .menu-toggle {
		color: var(--text-primary);
	}

	.mobile-menu {
		padding: 10px;
		border-bottom: 1px solid var(--line-dim);
		background: var(--panel-depth-1);
	}

	:global(.light) .mobile-menu {
		background: var(--panel-depth-1);
		border-bottom-color: var(--line-dim);
	}

	.mobile-category {
		margin-bottom: 6px;
	}

	.mobile-category .category-btn {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 12px 14px;
		background: transparent;
		border: none;
		color: var(--text-secondary);
		font-family: var(--font-display);
		font-size: 0.9375rem;
		font-weight: 600;
		border-radius: 8px;
		cursor: pointer;
		text-align: left;
	}

	:global(.light) .mobile-category .category-btn {
		color: var(--text-secondary);
	}

	.mobile-category .category-btn.active {
		color: var(--accent-glow);
		background: oklch(from var(--accent-glow) l c h / 0.1);
	}

	.mobile-sections {
		margin-left: 30px;
		padding: 6px 0;
	}

	.mobile-content {
		flex: 1;
		overflow-y: auto;
		padding: 20px;
	}

	/* ============================================
	   MODELS SECTION
	   ============================================ */
	.models-section {
		max-width: 860px;
	}

	.group-description {
		font-family: var(--font-display);
		font-size: 0.875rem;
		color: var(--text-secondary);
		margin-bottom: 18px;
		line-height: 1.5;
	}

	:global(.light) .group-description {
		color: var(--text-secondary);
	}

	.provider-section {
		margin-bottom: 18px;
	}

	.provider-section:last-child {
		margin-bottom: 0;
	}

	.provider-header {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--text-tertiary);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		margin-bottom: 12px;
		padding-bottom: 8px;
		border-bottom: 1px solid var(--line-subtle);
	}

	:global(.light) .provider-header {
		color: var(--text-tertiary);
		border-bottom-color: var(--line-subtle);
	}

	.model-cards {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
		gap: 12px;
	}

	.model-cards.tts-cards,
	.model-cards.stt-cards {
		grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
	}

	.model-card {
		display: flex;
		flex-direction: column;
		padding: 14px;
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 2px solid var(--line-dim);
		border-radius: 12px;
		cursor: pointer;
		transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
		text-align: left;
		gap: 8px;
		position: relative;
		overflow: hidden;
		/* Subtle inset effect */
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.03),
			inset 0 -1px 2px oklch(0 0 0 / 0.08);
	}

	/* Glow corner effect */
	.model-card::before {
		content: '';
		position: absolute;
		top: 0;
		right: 0;
		width: 50px;
		height: 50px;
		background: radial-gradient(
			circle at top right,
			oklch(from var(--accent-glow) l c h / 0.15) 0%,
			transparent 70%
		);
		opacity: 0;
		transition: opacity 0.3s ease;
	}

	/* Scanline overlay */
	.model-card::after {
		content: '';
		position: absolute;
		inset: 0;
		background: repeating-linear-gradient(
			0deg,
			transparent 0px,
			transparent 2px,
			oklch(0 0 0 / 0.015) 2px,
			oklch(0 0 0 / 0.015) 4px
		);
		pointer-events: none;
		opacity: 0.5;
	}

	:global(.light) .model-card {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.3),
			inset 0 -1px 2px oklch(0 0 0 / 0.03);
	}

	.model-card:hover:not(:disabled) {
		border-color: oklch(from var(--accent-glow) l c h / 0.6);
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		transform: translateY(-3px) scale(1.01);
		box-shadow:
			0 8px 28px oklch(0 0 0 / 0.25),
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.2),
			inset 0 0 20px -10px oklch(from var(--accent-glow) l c h / 0.1);
	}

	.model-card:hover::before {
		opacity: 1;
	}

	:global(.light) .model-card:hover:not(:disabled) {
		background:
			linear-gradient(180deg, oklch(0.99 0.005 250) 0%, var(--panel-depth-4) 100%);
		box-shadow:
			0 8px 28px oklch(0 0 0 / 0.12),
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.25);
	}

	.model-card.active {
		border-color: var(--accent-glow);
		background:
			linear-gradient(180deg, oklch(from var(--accent-glow) l c h / 0.12) 0%, oklch(from var(--accent-glow) l c h / 0.06) 100%);
		box-shadow:
			0 0 0 1px oklch(from var(--accent-glow) l c h / 0.3),
			0 0 20px -4px oklch(from var(--accent-glow) l c h / 0.3),
			0 6px 20px oklch(0 0 0 / 0.2);
		animation: pulseGlow 3s ease-in-out infinite;
	}

	.model-card.active::before {
		opacity: 1;
	}

	/* Status indicator for active card */
	.model-card.active .model-card-header::after {
		content: '';
		position: absolute;
		top: 10px;
		right: 10px;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--accent-glow);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-glow) l c h / 0.5);
		animation: statusPulse 2s ease-in-out infinite;
	}

	.model-card.deprecated {
		opacity: 0.65;
	}

	.model-card.deprecated::before {
		background: radial-gradient(
			circle at top right,
			oklch(from var(--accent-warm) l c h / 0.15) 0%,
			transparent 70%
		);
	}

	.model-card.deprecated:hover {
		border-color: oklch(from var(--accent-warm) l c h / 0.5);
	}

	.model-card:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.model-card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		position: relative;
	}

	.model-card-name {
		font-family: var(--font-display);
		font-size: 0.9375rem;
		font-weight: 700;
		color: var(--text-primary);
		transition: color 0.25s ease;
	}

	.model-card.active .model-card-name {
		color: var(--accent-glow);
	}

	:global(.light) .model-card-name {
		color: var(--text-primary);
	}

	.model-card-desc {
		font-family: var(--font-display);
		font-size: 0.8125rem;
		color: var(--text-secondary);
		line-height: 1.45;
		margin: 0;
		/* Show more info on hover */
		max-height: 2.9em;
		overflow: hidden;
		transition: max-height 0.3s ease;
	}

	.model-card:hover .model-card-desc {
		max-height: 6em;
	}

	:global(.light) .model-card-desc {
		color: var(--text-secondary);
	}

	.model-card-capabilities {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
		margin-top: 6px;
	}

	.capability-badge {
		font-family: var(--font-mono);
		font-size: 0.5625rem;
		font-weight: 600;
		padding: 4px 8px;
		border-radius: 4px;
		background: var(--panel-depth-1);
		color: var(--text-tertiary);
		letter-spacing: 0.03em;
		text-transform: uppercase;
		border: 1px solid var(--line-subtle);
		transition: all 0.2s ease;
		position: relative;
		z-index: 1;
	}

	.model-card:hover .capability-badge {
		border-color: var(--line-dim);
		box-shadow: 0 1px 3px oklch(0 0 0 / 0.1);
	}

	.capability-badge.highlight {
		background: oklch(from var(--accent-glow) l c h / 0.18);
		color: var(--accent-glow);
		border-color: oklch(from var(--accent-glow) l c h / 0.25);
		box-shadow: 0 0 8px -2px oklch(from var(--accent-glow) l c h / 0.2);
	}

	.capability-badge.audio {
		background: oklch(0.60 0.15 300 / 0.18);
		color: oklch(0.70 0.18 300);
		border-color: oklch(0.60 0.15 300 / 0.25);
	}

	.capability-badge.text-excellent {
		background: oklch(from var(--accent-success) l c h / 0.18);
		color: var(--accent-success);
		border-color: oklch(from var(--accent-success) l c h / 0.25);
	}

	.capability-badge.text-good {
		background: oklch(from var(--accent-glow) l c h / 0.18);
		color: var(--accent-glow);
		border-color: oklch(from var(--accent-glow) l c h / 0.25);
	}

	.capability-badge.text-basic {
		background: oklch(from var(--accent-warm) l c h / 0.18);
		color: var(--accent-warm);
		border-color: oklch(from var(--accent-warm) l c h / 0.25);
	}

	.model-card-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-top: auto;
		padding-top: 10px;
		border-top: 1px solid var(--line-subtle);
	}

	:global(.light) .model-card-footer {
		border-top-color: var(--line-subtle);
	}

	.model-card-price {
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--accent-glow);
	}

	.model-card-resolution {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--text-tertiary);
	}

	:global(.light) .model-card-resolution {
		color: var(--text-tertiary);
	}

	.model-card-formats {
		font-family: var(--font-mono);
		font-size: 0.625rem;
		color: var(--text-tertiary);
		font-style: normal;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	:global(.light) .model-card-formats {
		color: var(--text-tertiary);
	}

	.badge.warning {
		background: oklch(from var(--accent-warm) l c h / 0.15);
		color: var(--accent-warm);
	}

	/* ============================================
	   TTS SETTINGS - Audio Control Panel
	   ============================================ */
	.tts-settings {
		margin-top: 18px;
		padding: 20px;
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 1px solid var(--line-dim);
		border-radius: 12px;
		display: flex;
		flex-direction: column;
		gap: 18px;
		position: relative;
		overflow: hidden;
	}

	/* Panel header accent */
	.tts-settings::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		height: 3px;
		background: linear-gradient(
			90deg,
			var(--accent-glow) 0%,
			oklch(from var(--accent-glow) l c h / 0.3) 50%,
			var(--accent-glow) 100%
		);
	}

	/* Frequency indicator decoration */
	.tts-settings::after {
		content: '';
		position: absolute;
		top: 12px;
		right: 12px;
		width: 40px;
		height: 20px;
		background: repeating-linear-gradient(
			90deg,
			var(--accent-glow) 0px,
			var(--accent-glow) 2px,
			transparent 2px,
			transparent 6px
		);
		opacity: 0.3;
		border-radius: 2px;
	}

	:global(.light) .tts-settings {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
	}

	.tts-setting-row {
		display: flex;
		flex-direction: column;
		gap: 10px;
		position: relative;
		z-index: 1;
	}

	.tts-setting-row label {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-secondary);
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.tts-setting-row label span {
		font-weight: 600;
		color: var(--accent-glow);
		font-size: 0.75rem;
	}

	:global(.light) .tts-setting-row label {
		color: var(--text-secondary);
	}

	/* Industrial slider track */
	.slider {
		width: 100%;
		height: 8px;
		-webkit-appearance: none;
		appearance: none;
		background:
			linear-gradient(180deg, var(--panel-depth-1) 0%, oklch(from var(--panel-depth-1) calc(l + 0.02) c h) 100%);
		border-radius: 4px;
		outline: none;
		border: 1px solid var(--line-subtle);
		box-shadow:
			inset 0 1px 3px oklch(0 0 0 / 0.2),
			0 1px 0 oklch(1 0 0 / 0.03);
		position: relative;
	}

	:global(.light) .slider {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
	}

	/* Industrial slider thumb */
	.slider::-webkit-slider-thumb {
		-webkit-appearance: none;
		appearance: none;
		width: 22px;
		height: 22px;
		border-radius: 50%;
		background:
			linear-gradient(180deg, oklch(0.95 0 0) 0%, oklch(0.85 0 0) 100%);
		cursor: pointer;
		border: 2px solid var(--accent-glow);
		box-shadow:
			0 2px 8px oklch(0 0 0 / 0.3),
			0 0 0 3px oklch(from var(--accent-glow) l c h / 0.15),
			inset 0 1px 0 oklch(1 0 0 / 0.5);
		transition: all 0.2s ease;
	}

	.slider::-webkit-slider-thumb:hover {
		transform: scale(1.15);
		border-color: var(--accent-glow);
		box-shadow:
			0 4px 12px oklch(0 0 0 / 0.35),
			0 0 0 4px oklch(from var(--accent-glow) l c h / 0.25),
			0 0 15px oklch(from var(--accent-glow) l c h / 0.3),
			inset 0 1px 0 oklch(1 0 0 / 0.5);
	}

	.slider:active::-webkit-slider-thumb {
		background:
			linear-gradient(180deg, var(--accent-glow) 0%, oklch(from var(--accent-glow) calc(l - 0.1) c h) 100%);
		border-color: var(--accent-glow);
	}

	.slider::-moz-range-thumb {
		width: 22px;
		height: 22px;
		border-radius: 50%;
		background:
			linear-gradient(180deg, oklch(0.95 0 0) 0%, oklch(0.85 0 0) 100%);
		cursor: pointer;
		border: 2px solid var(--accent-glow);
		box-shadow:
			0 2px 8px oklch(0 0 0 / 0.3),
			0 0 0 3px oklch(from var(--accent-glow) l c h / 0.15);
	}

	/* Voice selector - Radio frequency tuner style */
	.tts-settings select.input {
		background:
			linear-gradient(180deg, var(--panel-depth-2) 0%, var(--panel-depth-1) 100%);
		border: 1px solid var(--line-dim);
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		font-weight: 500;
		letter-spacing: 0.02em;
		box-shadow:
			inset 0 1px 3px oklch(0 0 0 / 0.12),
			0 1px 0 oklch(1 0 0 / 0.03);
	}

	.tts-settings select.input:focus {
		border-color: var(--accent-glow);
		box-shadow:
			inset 0 1px 3px oklch(0 0 0 / 0.1),
			0 0 0 3px oklch(from var(--accent-glow) l c h / 0.15),
			0 0 12px -2px oklch(from var(--accent-glow) l c h / 0.25);
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

	/* ============================================
	   CREDENTIAL POLICIES - Access Control Panel
	   ============================================ */
	.policies-list {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}

	.policy-item {
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 1px solid var(--line-dim);
		border-radius: 12px;
		padding: 18px;
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 18px;
		transition: all 0.25s ease;
		position: relative;
		overflow: hidden;
	}

	/* Left accent - will change color based on status */
	.policy-item::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		width: 4px;
		height: 100%;
		background: var(--line-dim);
		transition: all 0.3s ease;
	}

	/* Connection line to status */
	.policy-item::after {
		content: '';
		position: absolute;
		top: 50%;
		left: 4px;
		width: 12px;
		height: 1px;
		background: var(--line-subtle);
		transform: translateY(-50%);
	}

	.policy-item:hover {
		border-color: var(--line-bright);
		box-shadow:
			0 4px 16px oklch(0 0 0 / 0.12),
			inset 0 0 30px -15px oklch(from var(--accent-glow) l c h / 0.06);
	}

	/* Status-based left accent colors */
	.policy-item.status-green::before {
		background: linear-gradient(180deg, var(--accent-success) 0%, oklch(from var(--accent-success) l c h / 0.4) 100%);
	}
	.policy-item.status-yellow::before {
		background: linear-gradient(180deg, var(--accent-warm) 0%, oklch(from var(--accent-warm) l c h / 0.4) 100%);
	}
	.policy-item.status-red::before {
		background: linear-gradient(180deg, var(--accent-danger) 0%, oklch(from var(--accent-danger) l c h / 0.4) 100%);
	}
	.policy-item.status-blue::before {
		background: linear-gradient(180deg, var(--accent-glow) 0%, oklch(from var(--accent-glow) l c h / 0.4) 100%);
	}

	:global(.light) .policy-item {
		background:
			linear-gradient(180deg, var(--panel-depth-4) 0%, var(--panel-depth-3) 100%);
		border-color: var(--line-dim);
	}

	.policy-info {
		flex: 1;
		min-width: 0;
		padding-left: 16px;
	}

	.policy-info h4 {
		margin: 0 0 6px 0;
		font-family: var(--font-display);
		font-size: 1rem;
		font-weight: 700;
		color: var(--text-primary);
	}

	.policy-description {
		margin: 0 0 12px 0;
		font-family: var(--font-display);
		font-size: 0.8125rem;
		color: var(--text-secondary);
		line-height: 1.5;
	}

	:global(.light) .policy-description {
		color: var(--text-secondary);
	}

	.policy-status {
		display: flex;
		align-items: center;
		gap: 8px;
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		padding: 6px 10px;
		background: var(--panel-depth-1);
		border-radius: 6px;
		border: 1px solid var(--line-subtle);
	}

	.status-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--text-tertiary);
		box-shadow: 0 0 6px 1px oklch(from var(--text-tertiary) l c h / 0.3);
		transition: all 0.3s ease;
	}

	.policy-status.status-green .status-dot {
		background: var(--accent-success);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-success) l c h / 0.5);
		animation: statusPulse 2s ease-in-out infinite;
	}
	.policy-status.status-yellow .status-dot {
		background: var(--accent-warm);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-warm) l c h / 0.5);
		animation: statusPulse 2.5s ease-in-out infinite;
	}
	.policy-status.status-red .status-dot {
		background: var(--accent-danger);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-danger) l c h / 0.5);
	}
	.policy-status.status-blue .status-dot {
		background: var(--accent-glow);
		box-shadow: 0 0 8px 2px oklch(from var(--accent-glow) l c h / 0.5);
		animation: statusPulse 2s ease-in-out infinite;
	}

	.policy-status.status-green .status-text { color: var(--accent-success); }
	.policy-status.status-yellow .status-text { color: var(--accent-warm); }
	.policy-status.status-red .status-text { color: var(--accent-danger); }
	.policy-status.status-blue .status-text { color: var(--accent-glow); }

	.policy-control {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 8px;
		flex-shrink: 0;
	}

	/* Industrial dropdown styling */
	.policy-control select {
		padding: 10px 36px 10px 14px;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 500;
		letter-spacing: 0.02em;
		border-radius: 8px;
		background:
			linear-gradient(180deg, var(--panel-depth-3) 0%, var(--panel-depth-2) 100%);
		border: 1px solid var(--line-dim);
		color: var(--text-primary);
		cursor: pointer;
		min-width: 180px;
		transition: all 0.25s ease;
		appearance: none;
		-webkit-appearance: none;
		-moz-appearance: none;
		background-image: url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%236b7a99' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M6 8l4 4 4-4'/%3e%3c/svg%3e");
		background-position: right 10px center;
		background-repeat: no-repeat;
		background-size: 16px;
		/* Industrial inset effect */
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.03),
			inset 0 -1px 2px oklch(0 0 0 / 0.1);
	}

	.policy-control select:hover {
		border-color: var(--line-bright);
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.05),
			inset 0 -1px 2px oklch(0 0 0 / 0.08),
			0 2px 8px oklch(0 0 0 / 0.1);
	}

	.policy-control select:focus {
		outline: none;
		border-color: var(--accent-glow);
		box-shadow:
			0 0 0 3px oklch(from var(--accent-glow) l c h / 0.15),
			0 0 12px -2px oklch(from var(--accent-glow) l c h / 0.2);
	}

	:global(.light) .policy-control select {
		background:
			linear-gradient(180deg, oklch(0.99 0 0) 0%, var(--panel-depth-4) 100%);
		border-color: var(--line-dim);
	}

	.policy-control select:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.saving-indicator {
		font-family: var(--font-mono);
		font-size: 0.625rem;
		color: var(--accent-glow);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		animation: statusPulse 1s ease-in-out infinite;
	}

	.policy-help {
		margin-top: 28px;
		padding: 18px;
		background: var(--panel-depth-2);
		border-radius: 10px;
		border: 1px solid var(--line-dim);
		position: relative;
	}

	.policy-help::before {
		content: '';
		position: absolute;
		top: 0;
		left: 0;
		width: 3px;
		height: 100%;
		background: var(--accent-glow-dim);
		border-radius: 3px 0 0 3px;
	}

	:global(.light) .policy-help {
		background: var(--panel-depth-3);
		border-color: var(--line-dim);
	}

	.policy-help h4 {
		margin: 0 0 14px 0;
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-tertiary);
	}

	:global(.light) .policy-help h4 {
		color: var(--text-tertiary);
	}

	.policy-help ul {
		margin: 0;
		padding: 0 0 0 22px;
		font-family: var(--font-display);
		font-size: 0.875rem;
		color: var(--text-secondary);
		line-height: 1.7;
	}

	:global(.light) .policy-help ul {
		color: var(--text-secondary);
	}

	.policy-help li {
		margin-bottom: 10px;
	}

	.policy-help li:last-child {
		margin-bottom: 0;
	}

	.policy-help strong {
		color: var(--text-primary);
		font-weight: 600;
	}
</style>
