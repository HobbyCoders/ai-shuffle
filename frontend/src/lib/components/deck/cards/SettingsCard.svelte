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
	import type { Project } from '$lib/stores/types';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig, changePassword } from '$lib/api/auth';
	import WebhookManager from '$lib/components/WebhookManager.svelte';
	import ConnectorManager from '$lib/components/ConnectorManager.svelte';
import RateLimitManager from '$lib/components/RateLimitManager.svelte';
	import ApiUserManager from '$lib/components/ApiUserManager.svelte';
	import { slide, fade } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences';
	import { timezonePreference } from '$lib/stores/timezonePreference';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import HotkeyCapture from '$lib/components/settings/HotkeyCapture.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { Settings } from 'lucide-svelte';
	import AIToolsSettings from '$lib/components/settings/AIToolsSettings.svelte';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// Navigation - simplified to 3 main tabs
	type Tab = 'account' | 'ai' | 'connectors' | 'system';
	let activeTab: Tab = $state('account');

	const settingsTabs = $derived([
		{ id: 'account' as Tab, label: 'Account' },
		{ id: 'ai' as Tab, label: 'AI Models' },
		...($isAdmin ? [{ id: 'connectors' as Tab, label: 'Connectors' }] : []),
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

	// AI settings are now managed by AIToolsSettings component

	// Password change
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let changingPassword = $state(false);
	let passwordChangeError = $state('');
	let passwordChangeSuccess = $state('');

	// Chat Behavior Preferences
	let chatPrefs = $state<ChatPreferences | null>(null);
	let currentTimezone = $state('local');

	// Security Instructions (admin only)
	let securityInstructions = $state('');
	let defaultSecurityInstructions = $state('');
	let originalSecurityInstructions = $state(''); // Track original to detect changes
	let isSecurityInstructionsCustomized = $state(false);
	let loadingSecurityInstructions = $state(false);
	let savingSecurityInstructions = $state(false);
	let securityInstructionsSuccess = $state('');
	let securityInstructionsError = $state('');

	// Load data on mount — use onMount for one-time setup to avoid
	// subscription accumulation from $effect re-runs.
	onMount(() => {
		if ($isAdmin) {
			loadAllData();
		}
		loadChatPreferences();
		timezonePreference.load();

		// Subscribe to chat preferences store
		const unsubscribe = chatPreferences.subscribe(prefs => {
			chatPrefs = prefs;
		});
		const unsubTz = timezonePreference.subscribe(tz => {
			currentTimezone = tz;
		});

		return () => { unsubscribe(); unsubTz(); };
	});

	async function loadChatPreferences() {
		await chatPreferences.load();
	}

	async function loadAllData() {
		await Promise.all([
			loadData(),
			loadAuthStatus(),
			loadWorkspaceConfig(),
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

	// Integration settings are now loaded by AIToolsSettings component

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
											<span>Theme</span>
											<span class="setting-desc">Choose your preferred appearance</span>
										</div>
										<div class="theme-switcher">
											<button
												class="theme-option"
												class:active={$themePreference === 'light'}
												onclick={() => theme.setTheme('light')}
												aria-label="Light theme"
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
												aria-label="Dark theme"
											>
												<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
													<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
												</svg>
											</button>
											<button
												class="theme-option"
												class:active={$themePreference === 'system'}
												onclick={() => theme.setTheme('system')}
												aria-label="System theme"
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
											<label for="settings-workspace-path">Workspace Path</label>
											<span class="setting-desc">Base directory for projects</span>
										</div>
										<div class="input-group">
											<input
												id="settings-workspace-path"
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

									<!-- Timezone -->
									<div class="setting-row">
										<div class="setting-info">
											<label for="settings-timezone">Timezone</label>
											<span class="setting-desc">Used for timestamps across the UI</span>
										</div>
										<select
											id="settings-timezone"
											class="input"
											value={currentTimezone}
											onchange={(e) => timezonePreference.setTimezone(e.currentTarget.value)}
										>
											<option value="local">Browser Default</option>
											<option value="UTC">UTC</option>
											<optgroup label="Americas">
												<option value="America/New_York">Eastern (New York)</option>
												<option value="America/Chicago">Central (Chicago)</option>
												<option value="America/Denver">Mountain (Denver)</option>
												<option value="America/Los_Angeles">Pacific (Los Angeles)</option>
												<option value="America/Anchorage">Alaska</option>
												<option value="Pacific/Honolulu">Hawaii</option>
												<option value="America/Sao_Paulo">S&atilde;o Paulo</option>
											</optgroup>
											<optgroup label="Europe &amp; Africa">
												<option value="Europe/London">London</option>
												<option value="Europe/Paris">Paris / Berlin</option>
												<option value="Europe/Moscow">Moscow</option>
												<option value="Africa/Cairo">Cairo</option>
											</optgroup>
											<optgroup label="Asia &amp; Pacific">
												<option value="Asia/Dubai">Dubai</option>
												<option value="Asia/Kolkata">India (Kolkata)</option>
												<option value="Asia/Shanghai">China (Shanghai)</option>
												<option value="Asia/Tokyo">Tokyo</option>
												<option value="Asia/Seoul">Seoul</option>
												<option value="Australia/Sydney">Sydney</option>
												<option value="Pacific/Auckland">Auckland</option>
											</optgroup>
										</select>
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
											<span>Change Password</span>
										</div>
										<div class="password-form">
											<input
												type="password"
												autocomplete="off"
												bind:value={currentPassword}
												placeholder="Current password"
												class="input"
											/>
											<input
												type="password"
												autocomplete="new-password"
												bind:value={newPassword}
												placeholder="New password (8+ chars)"
												class="input"
											/>
											<input
												type="password"
												autocomplete="new-password"
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
														autocomplete="off"
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
												<span>Show Thinking Blocks</span>
												<span class="setting-desc">Display Claude's reasoning process</span>
											</div>
											<button
												onclick={() => chatPreferences.setShowThinkingBlocks(!chatPrefs?.showThinkingBlocks)}
												class="toggle"
												class:active={chatPrefs.showThinkingBlocks}
												role="switch"
												aria-checked={chatPrefs.showThinkingBlocks}
												aria-label="Show Thinking Blocks"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Thinking Blocks Start Expanded -->
										<div class="setting-row">
											<div class="setting-info">
												<span>Expand Thinking by Default</span>
												<span class="setting-desc">Thinking blocks start expanded</span>
											</div>
											<button
												onclick={() => chatPreferences.setThinkingStartExpanded(!chatPrefs?.thinkingStartExpanded)}
												class="toggle"
												class:active={chatPrefs.thinkingStartExpanded}
												disabled={!chatPrefs.showThinkingBlocks}
												role="switch"
												aria-checked={chatPrefs.thinkingStartExpanded}
												aria-label="Expand Thinking by Default"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Thinking Summary on Collapse -->
										<div class="setting-row">
											<div class="setting-info">
												<span>Show Thinking Summary</span>
												<span class="setting-desc">Preview thinking content when collapsed</span>
											</div>
											<button
												onclick={() => chatPreferences.setShowThinkingSummary(!chatPrefs?.showThinkingSummary)}
												class="toggle"
												class:active={chatPrefs.showThinkingSummary}
												disabled={!chatPrefs.showThinkingBlocks}
												role="switch"
												aria-checked={chatPrefs.showThinkingSummary}
												aria-label="Show Thinking Summary"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Tools Start Expanded -->
										<div class="setting-row">
											<div class="setting-info">
												<span>Expand Tools by Default</span>
												<span class="setting-desc">Tool results start expanded</span>
											</div>
											<button
												onclick={() => chatPreferences.setToolsStartExpanded(!chatPrefs?.toolsStartExpanded)}
												class="toggle"
												class:active={chatPrefs.toolsStartExpanded}
												role="switch"
												aria-checked={chatPrefs.toolsStartExpanded}
												aria-label="Expand Tools by Default"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Todo List Start Expanded -->
										<div class="setting-row">
											<div class="setting-info">
												<span>Expand Todo List by Default</span>
												<span class="setting-desc">Task list starts expanded</span>
											</div>
											<button
												onclick={() => chatPreferences.setTodosStartExpanded(!chatPrefs?.todosStartExpanded)}
												class="toggle"
												class:active={chatPrefs.todosStartExpanded}
												role="switch"
												aria-checked={chatPrefs.todosStartExpanded}
												aria-label="Expand Todo List by Default"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<hr class="divider" />

										<!-- Auto-scroll on new messages -->
										<div class="setting-row">
											<div class="setting-info">
												<span>Auto-scroll on New Messages</span>
												<span class="setting-desc">Automatically scroll to new content</span>
											</div>
											<button
												onclick={() => chatPreferences.setAutoScrollOnNewMessage(!chatPrefs?.autoScrollOnNewMessage)}
												class="toggle"
												class:active={chatPrefs.autoScrollOnNewMessage}
												role="switch"
												aria-checked={chatPrefs.autoScrollOnNewMessage}
												aria-label="Auto-scroll on New Messages"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Show Message Timestamps -->
										<div class="setting-row">
											<div class="setting-info">
												<span>Show Message Timestamps</span>
												<span class="setting-desc">Display time on each message</span>
											</div>
											<button
												onclick={() => chatPreferences.setShowMessageTimestamps(!chatPrefs?.showMessageTimestamps)}
												class="toggle"
												class:active={chatPrefs.showMessageTimestamps}
												role="switch"
												aria-checked={chatPrefs.showMessageTimestamps}
												aria-label="Show Message Timestamps"
											>
												<span class="toggle-knob"></span>
											</button>
										</div>

										<!-- Compact Message View -->
										<div class="setting-row">
											<div class="setting-info">
												<span>Compact Message View</span>
												<span class="setting-desc">Reduce spacing between messages</span>
											</div>
											<button
												onclick={() => chatPreferences.setCompactMessageView(!chatPrefs?.compactMessageView)}
												class="toggle"
												class:active={chatPrefs.compactMessageView}
												role="switch"
												aria-checked={chatPrefs.compactMessageView}
												aria-label="Compact Message View"
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
											<span>Global Voice Input</span>
											<span class="setting-desc">Press hotkey anywhere to start voice-to-text</span>
										</div>
										<button
										role="switch"
										aria-checked={$hotkeyPreferences.globalSttEnabled}
										onclick={() => hotkeyPreferences.setGlobalSttEnabled(!$hotkeyPreferences.globalSttEnabled)}
										class="toggle"
										class:active={$hotkeyPreferences.globalSttEnabled}
										aria-label="Global Voice Input"
									>
										<span class="toggle-knob"></span>
									</button>
									</div>

									{#if $hotkeyPreferences.globalSttEnabled}
										<div class="setting-row vertical">
											<div class="setting-info">
												<span>Voice Input Hotkey</span>
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
						<AIToolsSettings />
					</div>
				{/if}

				<!-- CONNECTORS TAB -->
				{#if activeTab === 'connectors' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ConnectorManager />
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

						<!-- API Users Section -->
						<section class="section">
							<button class="section-header" onclick={() => toggleSection('api-users')}>
								<h3>API Users</h3>
								<svg class="chevron" class:expanded={expandedSections.has('api-users')} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="6 9 12 15 18 9"/>
								</svg>
							</button>
							{#if expandedSections.has('api-users')}
								<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
									<ApiUserManager />
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

						<!-- Webhooks moved to Connectors tab -->
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
		background: transparent;
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
		background: transparent;
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

	.setting-info label {
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
		background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
		color: var(--muted-foreground);
		border: 1px solid color-mix(in oklch, var(--muted-foreground) 30%, transparent);
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

	/* Dividers */
	.divider {
		border: none;
		border-top: 1px solid var(--border);
		margin: 12px 0;
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

	/* -----------------------------------------------------------------------
	   EMBEDDED MODE
	   ----------------------------------------------------------------------- */

	/* Hide the original tab bar when embedded (replaced by fp-tab-bar in header) */
	.settings.embedded .tab-bar {
		display: none;
	}


</style>
