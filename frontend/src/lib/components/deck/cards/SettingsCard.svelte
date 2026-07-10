<script lang="ts">
	/**
	 * SettingsCard - Redesigned Modern Settings UI
	 *
	 * Streamlined categories:
	 * - Account: General, Security, Connections (merged)
	 * - Connections: Claude/GitHub auth + Devices (cluster registry)
	 * - AI: API Keys & Model Selection (merged, compact)
	 * - API: Rate limits + API users (admin)
	 * - System: System prompt (admin)
	 */
	import { onMount } from 'svelte';
	import './card-design-system.css';
	import { auth, username, isAdmin } from '$lib/stores/auth.svelte';
	import { api } from '$lib/api/client';
	import type { Profile, WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import type { Project } from '$lib/stores/types';
	import {
		getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig, changePassword,
		startClaudeLogin, completeClaudeLogin,
		listClaudeAccounts, startAddClaudeAccount, completeAddClaudeAccount,
		activateClaudeAccount, renameClaudeAccount, deleteClaudeAccount,
		setClaudeAutoSwitchOnLimit,
		type ClaudeAccount
	} from '$lib/api/auth';
	import ConnectorManager from '$lib/components/ConnectorManager.svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { slide, fade } from 'svelte/transition';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { rateLimit } from '$lib/stores/rate-limit';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import StudioEditorShell from '$lib/components/layout/StudioEditorShell.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { User, MessageSquare, KeyRound, Boxes, Plug, SlidersHorizontal, ScrollText, Globe, Code2 } from 'lucide-svelte';
	import AIToolsSettings from './settings/AIToolsSettings.svelte';

	// Sub-components
	import GeneralSection from './settings/GeneralSection.svelte';
	import SecuritySection from './settings/SecuritySection.svelte';
	import ConnectionsSection from './settings/ConnectionsSection.svelte';
	import DevicesSection from './settings/DevicesSection.svelte';
	import ChatBehaviorSection from './settings/ChatBehaviorSection.svelte';
	import HotkeysSection from './settings/HotkeysSection.svelte';
	import BrowserSection from './settings/BrowserSection.svelte';
	import ApiTab from './settings/ApiTab.svelte';
	import SystemTab from './settings/SystemTab.svelte';
	import LogsTab from './settings/LogsTab.svelte';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// Navigation — rail mirrors the mockup's information architecture.
	type Tab = 'account' | 'chat' | 'connections' | 'ai' | 'connectors' | 'browser' | 'api' | 'system' | 'logs';
	let activeTab: Tab = $state('account');

	interface RailItem {
		id: string;
		label: string;
		icon?: typeof User;
		heading?: boolean;
		count?: number | string;
	}

	const settingsTabs: RailItem[] = $derived([
		{ id: 'account', label: 'Account', icon: User },
		{ id: 'chat', label: 'Chat & Input', icon: MessageSquare },
		{ id: 'connections', label: 'Connections', icon: KeyRound },
		{ id: 'ai', label: 'AI Models', icon: Boxes },
		...($isAdmin
			? [
					{ id: 'admin-divider', label: 'Admin', heading: true } as RailItem,
					{ id: 'connectors', label: 'Connectors', icon: Plug },
					{ id: 'browser', label: 'Browser', icon: Globe },
					{ id: 'api', label: 'API', icon: Code2 },
					{ id: 'system', label: 'System', icon: SlidersHorizontal },
					{ id: 'logs', label: 'Logs', icon: ScrollText }
				]
			: [])
	]);

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

	// ----- Multiple Claude accounts -----
	// The active-account list is server-side + encrypted; we load it on mount and
	// after every mutation so the UI always mirrors the server. `encryptionReady`
	// gates the whole list: when false the store can't be opened (admin re-login
	// needed) and ConnectionsSection shows a banner instead of rows.
	let claudeAccounts: ClaudeAccount[] = $state([]);
	let activeAccountId: string | null = $state(null);
	let accountsEncryptionReady = $state(true);
	let accountsLoading = $state(false);
	let accountsError = $state('');
	// When on, hitting a Claude usage limit auto-switches to another saved
	// account and retries. Server-owned (returned by the accounts list); off by default.
	let claudeAutoSwitchOnLimit = $state(false);
	// Add-account flow (mirrors the connect flow but never disturbs the active login).
	let addAccountOAuthUrl: string | null = $state(null);
	let addAccountState: string | null = $state(null);
	let addAccountCode = $state('');
	let addAccountName = $state('');
	let addAccountMakeActive = $state(false);
	let addAccountLoading = $state(false);
	let addAccountCompleting = $state(false);
	// The account row currently being switched to (drives its inline spinner).
	let switchingAccountId: string | null = $state(null);

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
			loadSecurityInstructions(),
			loadClaudeAccounts()
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
	//
	// `forceReauth` drives the recovery path: it deletes the existing (possibly
	// expired / unrefreshable) credential server-side and starts a fresh OAuth
	// flow, so a green-but-dead token can be replaced from the UI. The non-forced
	// path short-circuits when already authenticated.
	async function startClaudeOAuth(forceReauth = false) {
		claudeLoginLoading = true;
		try {
			const result = await startClaudeLogin(forceReauth);
			if (result.already_authenticated) {
				await auth.checkAuth();
				return;
			}
			if (result.success && result.oauth_url && result.state) {
				claudeOAuthUrl = result.oauth_url;
				claudeOAuthState = result.state;
				claudeAuthCode = '';
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

	// Force a fresh login even if a credential already exists on disk.
	function startClaudeReauth() {
		return startClaudeOAuth(true);
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

	// ----- Multiple Claude accounts -----

	// Refresh the account list. Called on mount and after every mutation so the
	// UI stays a mirror of the server (switching is instant server-side). Never
	// throws — a locked store just flips `accountsEncryptionReady` false.
	async function loadClaudeAccounts() {
		accountsLoading = true;
		accountsError = '';
		try {
			const res = await listClaudeAccounts();
			claudeAccounts = res.accounts;
			activeAccountId = res.active_account_id;
			accountsEncryptionReady = res.encryption_ready;
			claudeAutoSwitchOnLimit = res.auto_switch_on_limit;
		} catch (e: any) {
			// A locked store surfaces as a friendly banner via the flag, not an
			// error toast; only genuinely unexpected failures set accountsError.
			accountsError = e?.detail || 'Failed to load Claude accounts';
		} finally {
			accountsLoading = false;
		}
	}

	// Start OAuth for ADDING an account — opens the browser popup exactly like the
	// primary connect flow, but hits the /accounts/start endpoint so the current
	// login is untouched.
	async function startAddAccount() {
		addAccountLoading = true;
		accountsError = '';
		try {
			const result = await startAddClaudeAccount();
			if (result.success && result.oauth_url && result.state) {
				addAccountOAuthUrl = result.oauth_url;
				addAccountState = result.state;
				addAccountCode = '';
				window.open(result.oauth_url, '_blank', 'width=600,height=700');
			} else {
				accountsError = result.error || result.message || 'Failed to start sign-in';
			}
		} catch (e: any) {
			accountsError = e?.detail || 'Failed to start sign-in';
		} finally {
			addAccountLoading = false;
		}
	}

	async function completeAddAccount() {
		if (!addAccountCode.trim() || !addAccountState) return;
		addAccountCompleting = true;
		accountsError = '';
		try {
			await completeAddClaudeAccount(
				addAccountCode.trim(),
				addAccountState,
				addAccountName.trim() || undefined,
				addAccountMakeActive
			);
			// Reset the add-account flow.
			addAccountOAuthUrl = null;
			addAccountState = null;
			addAccountCode = '';
			addAccountName = '';
			const wasMadeActive = addAccountMakeActive;
			addAccountMakeActive = false;
			await loadClaudeAccounts();
			// If the new account became active, the app's live credentials changed —
			// refresh the token-valid stores.
			if (wasMadeActive) await auth.checkAuth();
		} catch (e: any) {
			// 409 = encryption locked while completing.
			accountsError = e?.detail || 'Failed to add account';
		} finally {
			addAccountCompleting = false;
		}
	}

	function cancelAddAccount() {
		addAccountOAuthUrl = null;
		addAccountState = null;
		addAccountCode = '';
		addAccountName = '';
		addAccountMakeActive = false;
		accountsError = '';
	}

	// Switch the active account, then re-check auth: the credentials the app runs
	// with just changed, so the token-valid stores must be refreshed.
	async function activateAccount(id: string) {
		switchingAccountId = id;
		accountsError = '';
		// Dim the usage indicator immediately — the shown numbers belong to the
		// outgoing account until the backend broadcasts the new one's snapshot.
		rateLimit.markStale();
		try {
			await activateClaudeAccount(id);
			await loadClaudeAccounts();
			await auth.checkAuth();
		} catch (e: any) {
			// 409 = encryption locked, 404 = unknown id.
			accountsError = e?.detail || 'Failed to switch account';
		} finally {
			switchingAccountId = null;
		}
	}

	// Optimistically flip the "auto-switch on usage limits" toggle, then persist.
	// On failure revert to the prior value and surface the reason via the shared
	// accounts error banner.
	async function toggleClaudeAutoSwitch() {
		const previous = claudeAutoSwitchOnLimit;
		const next = !previous;
		claudeAutoSwitchOnLimit = next;
		accountsError = '';
		try {
			const res = await setClaudeAutoSwitchOnLimit(next);
			claudeAutoSwitchOnLimit = res.auto_switch_on_limit;
		} catch (e: any) {
			claudeAutoSwitchOnLimit = previous;
			accountsError = e?.detail || 'Failed to update auto-switch setting';
		}
	}

	async function renameAccount(id: string, name: string) {
		accountsError = '';
		try {
			await renameClaudeAccount(id, name);
			await loadClaudeAccounts();
		} catch (e: any) {
			accountsError = e?.detail || 'Failed to rename account';
		}
	}

	// Delete surfaces the server's 409 "switch first" message verbatim so the user
	// knows why an active-account delete was refused. Deleting the active account
	// changed the app's credentials → re-check auth after a successful delete.
	async function deleteAccount(id: string) {
		accountsError = '';
		const wasActive = id === activeAccountId;
		try {
			await deleteClaudeAccount(id);
			await loadClaudeAccounts();
			if (wasActive) await auth.checkAuth();
		} catch (e: any) {
			accountsError = e?.detail || 'Failed to delete account';
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
			<!-- Content -->
			<div class="content">
				{#if error}
					<div transition:slide={{ duration: 200 }}>
						<Banner severity="error" message={error} dismissible onDismiss={() => (error = '')} />
					</div>
				{/if}

				<!-- ACCOUNT -->
				{#if activeTab === 'account'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<!-- Profile -->
						<div class="set-group">
							<div class="set-grouphead">
								<h3><User />Profile</h3>
								<p>Your account on this install.</p>
							</div>
							<div class="set-row">
								<div class="lab"><h4>Username</h4><p>The name you sign in with.</p></div>
								<div class="ctl"><span class="ro-val">{$username ?? '—'}</span></div>
							</div>
						</div>

						<GeneralSection
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
					</div>
				{/if}

				<!-- CHAT & INPUT -->
				{#if activeTab === 'chat'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ChatBehaviorSection {chatPrefs} />
						<HotkeysSection />
					</div>
				{/if}

				<!-- CONNECTIONS -->
				{#if activeTab === 'connections'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ConnectionsSection
							{claudeLoginLoading}
							{claudeOAuthUrl}
							{claudeAuthCode}
							onClaudeAuthCodeChange={(v) => claudeAuthCode = v}
							{claudeCompletingLogin}
							{githubUser}
							{githubToken}
							onGithubTokenChange={(v) => githubToken = v}
							{githubLoginLoading}
							onStartClaudeOAuth={() => startClaudeOAuth(false)}
							onStartClaudeReauth={startClaudeReauth}
							onCompleteClaudeOAuth={completeClaudeOAuth}
							{claudeAccounts}
							{activeAccountId}
							{accountsEncryptionReady}
							{accountsLoading}
							{accountsError}
							{addAccountOAuthUrl}
							{addAccountCode}
							onAddAccountCodeChange={(v) => addAccountCode = v}
							{addAccountName}
							onAddAccountNameChange={(v) => addAccountName = v}
							{addAccountMakeActive}
							onAddAccountMakeActiveChange={(v) => addAccountMakeActive = v}
							{claudeAutoSwitchOnLimit}
							onToggleClaudeAutoSwitch={toggleClaudeAutoSwitch}
							{addAccountLoading}
							{addAccountCompleting}
							{switchingAccountId}
							onStartAddAccount={startAddAccount}
							onCompleteAddAccount={completeAddAccount}
							onCancelAddAccount={cancelAddAccount}
							onActivateAccount={activateAccount}
							onRenameAccount={renameAccount}
							onDeleteAccount={deleteAccount}
							onConnectGithub={connectGithub}
							onDisconnectGithub={disconnectGithub}
						/>

						<!-- Devices (cluster registry) — cross-install / cloud-offload UI.
						     Admin-gated: it surfaces and rotates the shared cluster secret. -->
						{#if $isAdmin}
							<DevicesSection />
						{/if}
					</div>
				{/if}

				<!-- AI MODELS -->
				{#if activeTab === 'ai'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<AIToolsSettings />
					</div>
				{/if}

				<!-- CONNECTORS -->
				{#if activeTab === 'connectors' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ConnectorManager />
					</div>
				{/if}

				<!-- BROWSER -->
				{#if activeTab === 'browser' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<BrowserSection />
					</div>
				{/if}

				<!-- API -->
				{#if activeTab === 'api' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ApiTab />
					</div>
				{/if}

				<!-- SYSTEM -->
				{#if activeTab === 'system' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<SystemTab
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
					</div>
				{/if}

				<!-- LOGS -->
				{#if activeTab === 'logs' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<LogsTab />
					</div>
				{/if}
			</div>
{/snippet}

<StudioEditorShell
	title="Settings"
	subtitle="Account, connections, AI models & system"
	sections={settingsTabs}
	active={activeTab}
	onSelect={(id) => (activeTab = id as Tab)}
	railLabel="Settings sections"
>
	<div class="settings card-system">
		{@render settingsContent()}
	</div>
</StudioEditorShell>

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

	/* Hosted inside StudioEditorShell: the shell owns scroll, width (1240px),
	   padding and the side-rail. Here we just lay out the section stack. */
	.settings {
		display: flex;
		flex-direction: column;
		background: transparent;
		color: var(--card-foreground);
		font-family: var(--font-sans);
	}

	.content {
		min-width: 0;
	}

	/* Block flow so each .set-group's own margin-bottom governs spacing —
	   matches the 5 editors exactly (no extra flex gap stacking). */
	.tab-content {
		min-width: 0;
	}

	/* Read-only field value (e.g. username in Profile) */
	.ro-val {
		font-size: 13.5px;
		font-weight: 500;
		color: var(--foreground);
	}

	/* Alerts & Messages */
</style>
