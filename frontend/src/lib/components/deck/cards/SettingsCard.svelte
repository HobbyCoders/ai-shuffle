<script lang="ts">
	/**
	 * SettingsCard - Redesigned Modern Settings UI
	 *
	 * Streamlined categories:
	 * - Account: General, Security, Connections (merged)
	 * - AI: API Keys & Model Selection (merged, compact)
	 * - System: Users, Webhooks, Cleanup (merged)
	 */
	import { onMount } from 'svelte';
	import './card-design-system.css';
	import { auth, username, isAdmin, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth';
	import { api } from '$lib/api/client';
	import { theme, themePreference, isDark, currentPresetId, themePresets, type Theme } from '$lib/stores/theme';
	import type { Profile, WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import type { Project } from '$lib/stores/appState';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig, changePassword } from '$lib/api/auth';
	import WebhookManager from '$lib/components/WebhookManager.svelte';
	import TwoFactorSetup from '$lib/components/TwoFactorSetup.svelte';
	import RateLimitManager from '$lib/components/RateLimitManager.svelte';
	import { slide, fade } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import HotkeyCapture from '$lib/components/settings/HotkeyCapture.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { Settings } from 'lucide-svelte';
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
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// Navigation - simplified to 3 main tabs
	type Tab = 'account' | 'ai' | 'system';
	let activeTab: Tab = $state('account');

	const settingsTabs = $derived([
		{ id: 'account' as Tab, label: 'Account' },
		{ id: 'ai' as Tab, label: 'AI Models' },
		...($isAdmin ? [{ id: 'system' as Tab, label: 'System' }] : [])
	]);

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
	let profiles: Profile[] = $state([]);
	let projects: Project[] = $state([]);
	let loading = $state(true);
	let error = $state('');

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

	let geminiApiKey = $state('');
	let geminiApiKeyMasked = $state('');
	let savingGeminiKey = $state(false);
	let geminiKeySuccess = $state('');
	let geminiKeyError = $state('');

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

	// Chat Behavior Preferences
	let chatPrefs = $state<ChatPreferences | null>(null);

	// Security Instructions (admin only)
	let securityInstructions = $state('');
	let defaultSecurityInstructions = $state('');
	let originalSecurityInstructions = $state(''); // Track original to detect changes
	let isSecurityInstructionsCustomized = $state(false);
	let loadingSecurityInstructions = $state(false);
	let savingSecurityInstructions = $state(false);
	let securityInstructionsSuccess = $state('');
	let securityInstructionsError = $state('');

	// Load data on mount
	$effect(() => {
		if ($isAdmin) {
			loadAllData();
		}
		loadChatPreferences();

		// Subscribe to chat preferences store
		const unsubscribe = chatPreferences.subscribe(prefs => {
			chatPrefs = prefs;
		});

		return () => unsubscribe();
	});

	async function loadChatPreferences() {
		await chatPreferences.load();
	}

	async function loadAllData() {
		await Promise.all([
			loadData(),
			loadAuthStatus(),
			loadWorkspaceConfig(),
			loadIntegrationSettings(),
			loadSecurityInstructions()
		]);
	}

	async function loadData() {
		loading = true;
		error = '';
		try {
			const [profilesRes, projectsRes] = await Promise.all([
				api.get<Profile[]>('/profiles'),
				api.get<Project[]>('/projects')
			]);
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
				image_api_key_set: boolean;
				image_api_key_masked: string;
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
			geminiApiKeyMasked = settings.image_api_key_masked || '';
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}
	}

	// Security Instructions functions
	async function loadSecurityInstructions() {
		loadingSecurityInstructions = true;
		securityInstructionsError = '';
		try {
			const response = await api.get<{
				current: string;
				default: string;
				is_customized: boolean;
			}>('/settings/security-instructions');
			securityInstructions = response.current;
			defaultSecurityInstructions = response.default;
			originalSecurityInstructions = response.current;
			isSecurityInstructionsCustomized = response.is_customized;
		} catch (e: any) {
			securityInstructionsError = e.detail || 'Failed to load security instructions';
			console.error('Failed to load security instructions:', e);
		} finally {
			loadingSecurityInstructions = false;
		}
	}

	async function saveSecurityInstructions() {
		if (!securityInstructions.trim()) {
			securityInstructionsError = 'Instructions cannot be empty. Use Revert to restore defaults.';
			return;
		}
		savingSecurityInstructions = true;
		securityInstructionsError = '';
		securityInstructionsSuccess = '';
		try {
			await api.put('/settings/security-instructions', {
				instructions: securityInstructions
			});
			originalSecurityInstructions = securityInstructions;
			isSecurityInstructionsCustomized = true;
			securityInstructionsSuccess = 'Saved successfully';
			setTimeout(() => securityInstructionsSuccess = '', 3000);
		} catch (e: any) {
			securityInstructionsError = e.detail || 'Failed to save security instructions';
		} finally {
			savingSecurityInstructions = false;
		}
	}

	async function revertSecurityInstructions() {
		if (!confirm('Are you sure you want to revert to the default security instructions? This will discard any customizations.')) {
			return;
		}
		savingSecurityInstructions = true;
		securityInstructionsError = '';
		securityInstructionsSuccess = '';
		try {
			await api.delete('/settings/security-instructions');
			securityInstructions = defaultSecurityInstructions;
			originalSecurityInstructions = defaultSecurityInstructions;
			isSecurityInstructionsCustomized = false;
			securityInstructionsSuccess = 'Reverted to default';
			setTimeout(() => securityInstructionsSuccess = '', 3000);
		} catch (e: any) {
			securityInstructionsError = e.detail || 'Failed to revert security instructions';
		} finally {
			savingSecurityInstructions = false;
		}
	}

	// Computed: check if instructions have changed
	let securityInstructionsChanged = $derived(securityInstructions !== originalSecurityInstructions);

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

	async function saveGeminiApiKey() {
		if (!geminiApiKey.trim()) return;
		savingGeminiKey = true;
		geminiKeyError = '';
		geminiKeySuccess = '';
		try {
			const result = await api.post<{success: boolean, masked_key: string}>('/settings/integrations/gemini', {
				api_key: geminiApiKey
			});
			geminiApiKeyMasked = result.masked_key;
			geminiKeySuccess = 'Saved';
			geminiApiKey = '';
			await loadIntegrationSettings();
			setTimeout(() => geminiKeySuccess = '', 3000);
		} catch (e: any) {
			geminiKeyError = e.detail || 'Failed to save';
		}
		savingGeminiKey = false;
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
			await api.post('/auth/github/login', { token: githubToken });
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
			await api.post('/auth/github/logout');
			githubUser = null;
			await auth.checkAuth();
		} catch (e: any) {
			error = e.detail || 'Failed to disconnect';
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

						<!-- Chat Behavior Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('chat-behavior')}>
								<h3>Chat Behavior</h3>
								<svg class="chevron" class:expanded={expandedSections.has('chat-behavior')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('chat-behavior')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									{#if chatPrefs}
										<!-- Show Thinking Blocks -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Show Thinking Blocks</label>
												<span class="setting-desc">Display Claude's reasoning process</span>
											</div>
											<button
												onclick={() => chatPreferences.setShowThinkingBlocks(!chatPrefs?.showThinkingBlocks)}
												class="toggle"
												class:active={chatPrefs.showThinkingBlocks}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Thinking Blocks Start Expanded -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Expand Thinking by Default</label>
												<span class="setting-desc">Thinking blocks start expanded</span>
											</div>
											<button
												onclick={() => chatPreferences.setThinkingStartExpanded(!chatPrefs?.thinkingStartExpanded)}
												class="toggle"
												class:active={chatPrefs.thinkingStartExpanded}
												disabled={!chatPrefs.showThinkingBlocks}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Thinking Summary on Collapse -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Show Thinking Summary</label>
												<span class="setting-desc">Preview thinking content when collapsed</span>
											</div>
											<button
												onclick={() => chatPreferences.setShowThinkingSummary(!chatPrefs?.showThinkingSummary)}
												class="toggle"
												class:active={chatPrefs.showThinkingSummary}
												disabled={!chatPrefs.showThinkingBlocks}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Tools Start Expanded -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Expand Tools by Default</label>
												<span class="setting-desc">Tool results start expanded</span>
											</div>
											<button
												onclick={() => chatPreferences.setToolsStartExpanded(!chatPrefs?.toolsStartExpanded)}
												class="toggle"
												class:active={chatPrefs.toolsStartExpanded}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Todo List Start Expanded -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Expand Todo List by Default</label>
												<span class="setting-desc">Task list starts expanded</span>
											</div>
											<button
												onclick={() => chatPreferences.setTodosStartExpanded(!chatPrefs?.todosStartExpanded)}
												class="toggle"
												class:active={chatPrefs.todosStartExpanded}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<hr class="divider" />

										<!-- Auto-scroll on new messages -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Auto-scroll on New Messages</label>
												<span class="setting-desc">Automatically scroll to new content</span>
											</div>
											<button
												onclick={() => chatPreferences.setAutoScrollOnNewMessage(!chatPrefs?.autoScrollOnNewMessage)}
												class="toggle"
												class:active={chatPrefs.autoScrollOnNewMessage}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Show Message Timestamps -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Show Message Timestamps</label>
												<span class="setting-desc">Display time on each message</span>
											</div>
											<button
												onclick={() => chatPreferences.setShowMessageTimestamps(!chatPrefs?.showMessageTimestamps)}
												class="toggle"
												class:active={chatPrefs.showMessageTimestamps}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Compact Message View -->
										<div class="setting-row">
											<div class="setting-info">
												<label>Compact Message View</label>
												<span class="setting-desc">Reduce spacing between messages</span>
											</div>
											<button
												onclick={() => chatPreferences.setCompactMessageView(!chatPrefs?.compactMessageView)}
												class="toggle"
												class:active={chatPrefs.compactMessageView}
											>
												<span class="toggle-knob"></span>
											</button>
										</div>
									{:else}
										<div class="loading-text">Loading preferences...</div>
									{/if}
								</div>
							{/if}
						</section>

						<!-- Hotkeys Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('hotkeys')}>
								<h3>Hotkeys</h3>
								<svg class="chevron" class:expanded={expandedSections.has('hotkeys')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('hotkeys')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<!-- Global Voice Input -->
									<div class="setting-row">
										<div class="setting-info">
											<label>Global Voice Input</label>
											<span class="setting-desc">Press hotkey anywhere to start voice-to-text</span>
										</div>
										<button
										role="switch"
										aria-checked={$hotkeyPreferences.globalSttEnabled}
										onclick={() => hotkeyPreferences.setGlobalSttEnabled(!$hotkeyPreferences.globalSttEnabled)}
										class="toggle"
										class:active={$hotkeyPreferences.globalSttEnabled}
									>
										<span class="toggle-knob"></span>
									</button>
									</div>

									{#if $hotkeyPreferences.globalSttEnabled}
										<div class="setting-row vertical">
											<div class="setting-info">
												<label>Voice Input Hotkey</label>
												<span class="setting-desc">Click to capture a new key combination</span>
											</div>
											<HotkeyCapture
												value={$hotkeyPreferences.globalSttHotkey}
												onChange={(v) => hotkeyPreferences.setGlobalSttHotkey(v)}
											/>
										</div>
									{/if}

									<div class="setting-hint">
										When triggered, transcribed text is copied to clipboard. If a text field is focused, it will also be pasted automatically.
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

									<!-- Google Gemini -->
									<div class="api-key-row">
										<div class="api-key-info">
											<span class="api-key-name">Google Gemini</span>
											{#if geminiApiKeyMasked}
												<code class="masked-key">{geminiApiKeyMasked}</code>
											{/if}
										</div>
										<div class="api-key-input">
											<input
												type="password"
												bind:value={geminiApiKey}
												placeholder={geminiApiKeyMasked ? 'Enter new key...' : 'AIza...'}
												class="input compact"
											/>
											<button
												onclick={saveGeminiApiKey}
												disabled={savingGeminiKey || !geminiApiKey.trim()}
												class="btn primary compact"
											>
												{savingGeminiKey ? '...' : 'Save'}
											</button>
										</div>
										{#if geminiKeySuccess}<span class="inline-success">{geminiKeySuccess}</span>{/if}
										{#if geminiKeyError}<span class="inline-error">{geminiKeyError}</span>{/if}
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

						<!-- Security Instructions Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('security-instructions')}>
								<h3>System Prompt Instructions</h3>
								<svg class="chevron" class:expanded={expandedSections.has('security-instructions')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('security-instructions')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<p class="setting-desc mb-3">
										These instructions are prepended to all system prompts sent to Claude.
										Use this to add custom instructions that apply across all profiles.
									</p>

									{#if loadingSecurityInstructions}
										<div class="loading-text">Loading...</div>
									{:else}
										<div class="security-instructions-editor">
											<textarea
												bind:value={securityInstructions}
												class="input security-textarea"
												placeholder="Enter security instructions..."
												rows="12"
											></textarea>

											<div class="security-instructions-footer">
												<div class="security-status">
													{#if isSecurityInstructionsCustomized}
														<span class="badge primary">Customized</span>
													{:else}
														<span class="badge muted">Using Default</span>
													{/if}
													{#if securityInstructionsChanged}
														<span class="badge warning">Unsaved changes</span>
													{/if}
												</div>

												<div class="security-actions">
													<button
														onclick={revertSecurityInstructions}
														class="btn secondary compact"
														disabled={!isSecurityInstructionsCustomized || savingSecurityInstructions}
														title="Revert to default instructions"
													>
														Revert to Default
													</button>
													<button
														onclick={saveSecurityInstructions}
														class="btn primary compact"
														disabled={!securityInstructionsChanged || savingSecurityInstructions}
													>
														{savingSecurityInstructions ? 'Saving...' : 'Save Changes'}
													</button>
												</div>
											</div>

											{#if securityInstructionsError}
												<p class="status error">{securityInstructionsError}</p>
											{/if}
											{#if securityInstructionsSuccess}
												<p class="status success">{securityInstructionsSuccess}</p>
											{/if}
										</div>
									{/if}
								</div>
							{/if}
						</section>

						<!-- Webhooks Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('webhooks')}>
								<h3>Webhooks</h3>
								<svg class="chevron" class:expanded={expandedSections.has('webhooks')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('webhooks')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<WebhookManager />
								</div>
							{/if}
						</section>
					</div>
				{/if}
			</div>
{/snippet}

<FeaturePageLayout icon={Settings} title="Settings">
	{#snippet headerTabs()}
		<div class="fp-tab-bar">
			{#each settingsTabs as tab}
				<button class="fp-tab" class:active={activeTab === tab.id} onclick={() => activeTab = tab.id}>
					{tab.label}
				</button>
			{/each}
		</div>
	{/snippet}

	<div class="settings card-system embedded">
		{@render settingsContent()}
	</div>
</FeaturePageLayout>

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
		font-family: var(--font-sans);
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
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.tab-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.tab-btn.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
		box-shadow: var(--selection-shadow);
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
		background: var(--hover-overlay);
	}

	.section-header:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	/* Center section header text on desktop */
	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}

		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
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

	.btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
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
		background: var(--hover-overlay);
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
		border-radius: var(--radius, 0.75rem);
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
	}

	.toggle:hover {
		border-color: var(--ring);
	}

	.toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.toggle.small {
		width: 36px;
		height: 20px;
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
	}

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
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
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.theme-option:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
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
		border: 1px solid color-mix(in oklch, var(--success) 30%, transparent);
	}

	.badge.muted {
		background: var(--muted);
		color: var(--muted-foreground);
		border: 1px solid var(--border);
	}

	.badge.primary {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.badge.warning {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		color: var(--warning);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
	}

	/* Security Instructions Editor */
	.security-instructions-editor {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.security-textarea {
		width: 100%;
		min-height: 200px;
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		line-height: 1.5;
		resize: vertical;
	}

	.security-instructions-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		flex-wrap: wrap;
		gap: 12px;
	}

	.security-status {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.security-actions {
		display: flex;
		gap: 8px;
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
		border-radius: var(--radius, 0.75rem);
		color: var(--primary-foreground);
	}

	.connection-icon.claude {
		background: linear-gradient(135deg, var(--warning), color-mix(in oklch, var(--warning) 80%, oklch(0.3 0 0)));
	}

	.connection-icon.github {
		background: var(--muted-foreground);
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
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
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
		border-radius: calc(var(--radius, 0.75rem) * 0.25);
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
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.icon-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.icon-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
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
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		font-family: var(--font-mono);
		font-size: 0.75rem;
		word-break: break-all;
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

	/* -----------------------------------------------------------------------
	   EMBEDDED MODE
	   ----------------------------------------------------------------------- */

	/* Hide the original tab bar when embedded (replaced by fp-tab-bar in header) */
	.settings.embedded .tab-bar {
		display: none;
	}


</style>
