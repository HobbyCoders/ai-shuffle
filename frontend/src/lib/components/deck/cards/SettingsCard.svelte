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
	import { auth, username, isAdmin, claudeAuthenticated, githubAuthenticated } from '$lib/stores/auth.svelte';
	import { api } from '$lib/api/client';
	import type { Profile, WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import type { Project } from '$lib/stores/types';
	import { getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig, changePassword, startClaudeLogin, completeClaudeLogin } from '$lib/api/auth';
	import ConnectorManager from '$lib/components/ConnectorManager.svelte';
	import { slide, fade } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { Settings } from 'lucide-svelte';
	import AIToolsSettings from '$lib/components/settings/AIToolsSettings.svelte';

	// Sub-components
	import GeneralSection from './settings/GeneralSection.svelte';
	import SecuritySection from './settings/SecuritySection.svelte';
	import ConnectionsSection from './settings/ConnectionsSection.svelte';
	import ChatBehaviorSection from './settings/ChatBehaviorSection.svelte';
	import HotkeysSection from './settings/HotkeysSection.svelte';
	import SystemTab from './settings/SystemTab.svelte';
	import LogsTab from './settings/LogsTab.svelte';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// Navigation - simplified to 3 main tabs
	type Tab = 'account' | 'ai' | 'connectors' | 'system' | 'logs';
	let activeTab: Tab = $state('account');

	const settingsTabs = $derived([
		{ id: 'account' as Tab, label: 'Account' },
		{ id: 'ai' as Tab, label: 'AI Models' },
		...($isAdmin ? [{ id: 'connectors' as Tab, label: 'Connectors' }] : []),
		...($isAdmin ? [{ id: 'system' as Tab, label: 'System' }] : []),
		...($isAdmin ? [{ id: 'logs' as Tab, label: 'Logs' }] : [])
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
	// Pending "revert security instructions" awaiting confirmation (replaces native confirm()).
	let pendingRevertSecurity = $state(false);

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

	function revertSecurityInstructions() {
		pendingRevertSecurity = true;
	}

	async function confirmRevertSecurityInstructions() {
		pendingRevertSecurity = false;
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
			const result = await startClaudeLogin();
			if (result.already_authenticated) {
				await auth.checkAuth();
				return;
			}
			if (result.success && result.oauth_url && result.state) {
				claudeOAuthUrl = result.oauth_url;
				claudeOAuthState = result.state;
				window.open(result.oauth_url, '_blank', 'width=600,height=700');
			} else {
				error = result.error || result.message || 'Failed to start OAuth';
			}
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
			const result = await completeClaudeLogin(claudeAuthCode.trim(), claudeOAuthState);
			if (!result.success) {
				error = result.error || result.message || 'Failed to complete OAuth';
				return;
			}
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
						<GeneralSection
							expanded={expandedSections.has('general')}
							onToggle={() => toggleSection('general')}
							{workspacePath}
							onWorkspacePathChange={(v) => workspacePath = v}
							{workspaceValidation}
							{validatingWorkspace}
							{savingWorkspace}
							{workspaceSuccess}
							{workspaceError}
							{currentTimezone}
							onValidateWorkspace={handleValidateWorkspace}
							onSaveWorkspace={handleSaveWorkspace}
						/>

						<SecuritySection
							expanded={expandedSections.has('security')}
							onToggle={() => toggleSection('security')}
							{currentPassword}
							onCurrentPasswordChange={(v) => currentPassword = v}
							{newPassword}
							onNewPasswordChange={(v) => newPassword = v}
							{confirmPassword}
							onConfirmPasswordChange={(v) => confirmPassword = v}
							{changingPassword}
							{passwordChangeSuccess}
							{passwordChangeError}
							onChangePassword={handleChangePassword}
						/>

						<ConnectionsSection
							expanded={expandedSections.has('connections')}
							onToggle={() => toggleSection('connections')}
							{claudeLoginLoading}
							{claudeOAuthUrl}
							{claudeAuthCode}
							onClaudeAuthCodeChange={(v) => claudeAuthCode = v}
							{claudeCompletingLogin}
							{githubUser}
							{githubToken}
							onGithubTokenChange={(v) => githubToken = v}
							{githubLoginLoading}
							onStartClaudeOAuth={startClaudeOAuth}
							onCompleteClaudeOAuth={completeClaudeOAuth}
							onConnectGithub={connectGithub}
							onDisconnectGithub={disconnectGithub}
						/>

						<ChatBehaviorSection
							expanded={expandedSections.has('chat-behavior')}
							onToggle={() => toggleSection('chat-behavior')}
							{chatPrefs}
						/>

						<HotkeysSection
							expanded={expandedSections.has('hotkeys')}
							onToggle={() => toggleSection('hotkeys')}
						/>
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
					<SystemTab
						{expandedSections}
						onToggleSection={toggleSection}
						{securityInstructions}
						onSecurityInstructionsChange={(v) => securityInstructions = v}
						{defaultSecurityInstructions}
						{isSecurityInstructionsCustomized}
						{securityInstructionsChanged}
						{loadingSecurityInstructions}
						{savingSecurityInstructions}
						{securityInstructionsSuccess}
						{securityInstructionsError}
						onSaveSecurityInstructions={saveSecurityInstructions}
						onRevertSecurityInstructions={revertSecurityInstructions}
					/>
				{/if}

				<!-- LOGS TAB -->
				{#if activeTab === 'logs' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<LogsTab />
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

<!-- Revert security instructions confirmation -->
<ConfirmDialog
	open={pendingRevertSecurity}
	title="Revert to defaults"
	message="Are you sure you want to revert to the default security instructions? This will discard any customizations."
	confirmLabel="Revert"
	danger
	onConfirm={confirmRevertSecurityInstructions}
	onCancel={() => (pendingRevertSecurity = false)}
/>

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

	/* -----------------------------------------------------------------------
	   EMBEDDED MODE
	   ----------------------------------------------------------------------- */

	/* Hide the original tab bar when embedded (replaced by fp-tab-bar in header) */
	.settings.embedded .tab-bar {
		display: none;
	}


</style>
