<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * SettingsCard - Redesigned Modern Settings UI
	 *
	 * Streamlined categories:
	 * - Account: Appearance, workspace, and timezone
	 * - Connections: Claude/GitHub auth + Devices (cluster registry)
	 * - AI: API Keys & Model Selection (merged, compact)
	 * - API: Rate limits + API users (admin)
	 * - System: System prompt (admin)
	 */
	import { onMount } from 'svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import './card-design-system.css';
	import { auth, isAdmin } from '$lib/stores/auth.svelte';
	import { api } from '$lib/api/client';
	import type { WorkspaceConfig, WorkspaceValidation } from '$lib/api/client';
	import {
		getWorkspaceConfig, validateWorkspacePath, setWorkspaceConfig
	} from '$lib/api/auth';
	import ConnectorManager from '$lib/components/ConnectorManager.svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { slide, fade } from 'svelte/transition';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import StudioEditorShell from '$lib/components/layout/StudioEditorShell.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { User, MessageSquare, KeyRound, Plug, SlidersHorizontal, ScrollText, Code2, AudioLines, Download } from 'lucide-svelte';

	// Sub-components
	import GeneralSection from './settings/GeneralSection.svelte';
	import DownloadsSection from './settings/DownloadsSection.svelte';
	import ProvidersSection from './settings/providers/ProvidersSection.svelte';
	import DevicesSection from './settings/DevicesSection.svelte';
	import ChatBehaviorSection from './settings/ChatBehaviorSection.svelte';
	import HotkeysSection from './settings/HotkeysSection.svelte';
	import VoiceSection from './settings/VoiceSection.svelte';
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
	type Tab = 'account' | 'chat' | 'voice' | 'providers' | 'downloads' | 'connectors' | 'api' | 'system' | 'logs';
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
		{ id: 'voice', label: 'Voice', icon: AudioLines },
		{ id: 'providers', label: 'Providers', icon: KeyRound },
		{ id: 'downloads', label: 'Downloads', icon: Download },
		...($isAdmin
			? [
					{ id: 'admin-divider', label: 'Admin', heading: true } as RailItem,
					{ id: 'connectors', label: 'Connectors', icon: Plug },
					{ id: 'api', label: 'API', icon: Code2 },
					{ id: 'system', label: 'System', icon: SlidersHorizontal },
					{ id: 'logs', label: 'Logs', icon: ScrollText }
				]
			: [])
	]);

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
	// Claude native-auth (connect / saved accounts / API key) now lives in the
	// providers store (Chunk 4) and is consumed directly by the providers/ UI —
	// SettingsCard no longer holds or drills that state. GitHub stays here.
	let githubToken = $state('');
	let githubLoginLoading = $state(false);
	let githubUser: string | null = $state(null);

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
			loadAuthStatus(),
			loadWorkspaceConfig(),
			loadSecurityInstructions()
		]);
	}

	async function loadAuthStatus() {
		try {
			const ghStatus = await api.get<{authenticated: boolean, user: string | null}>('/auth/github/status');
			githubUser = ghStatus.user;
		} catch (e) {
			console.error('Failed to load GitHub status:', e);
		}
	}

	async function loadWorkspaceConfig(quiet = false) {
		const before = workspacePath;
		const clean = !savingWorkspace && workspacePath === (workspaceConfig?.workspace_path ?? '');
		try {
			const fresh = await getWorkspaceConfig();
			if (!quiet || (clean && !savingWorkspace && before === workspacePath)) {
				workspaceConfig = fresh;
				workspacePath = fresh.workspace_path;
			} else if (workspaceConfig?.workspace_path !== fresh.workspace_path) {
				remoteChange = 'Workspace settings changed elsewhere. Your unsaved path is kept here.';
			}
		} catch (e) {
			console.error('Failed to load workspace config:', e);
		}
	}

	// Security Instructions functions
	async function loadSecurityInstructions(quiet = false) {
		const before = securityInstructions;
		const clean = !savingSecurityInstructions && securityInstructions === originalSecurityInstructions;
		if (!quiet) loadingSecurityInstructions = true;
		securityInstructionsError = '';
		try {
			const response = await api.get<{
				current: string;
				default: string;
				is_customized: boolean;
			}>('/settings/security-instructions');
			if (quiet && (!clean || savingSecurityInstructions || before !== securityInstructions)) {
				if (response.current !== originalSecurityInstructions) remoteChange = 'Security instructions changed elsewhere. Your unsaved instructions are kept here.';
				return;
			}
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

	// Claude native-auth handlers (connect / re-auth / saved accounts) moved to the
	// providers store in Chunk 4; the providers/ UI calls the store directly.

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

	let remoteChange = $state('');
	watchResources(['settings'], async () => {
		await Promise.all([
			loadAuthStatus(), loadWorkspaceConfig(true),
			...($isAdmin ? [loadSecurityInstructions(true), providers.refreshAll(), providers.refreshProviders()] : [])
		]);
	});
</script>

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}

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
					</div>
				{/if}

				<!-- DOWNLOADS -->
				{#if activeTab === 'downloads'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<DownloadsSection />
					</div>
				{/if}

				<!-- CHAT & INPUT -->
				{#if activeTab === 'chat'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ChatBehaviorSection {chatPrefs} />
						<HotkeysSection />
					</div>
				{/if}

				<!-- VOICE -->
				{#if activeTab === 'voice'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<VoiceSection />
					</div>
				{/if}

				<!-- PROVIDERS -->
				{#if activeTab === 'providers'}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ProvidersSection
							{githubUser}
							{githubToken}
							onGithubTokenChange={(v) => githubToken = v}
							{githubLoginLoading}
							onConnectGithub={connectGithub}
							onDisconnectGithub={disconnectGithub}
						/>

						<!-- Device management is admin-only: account linking, pairing, and runner access. -->
						{#if $isAdmin}
							<DevicesSection />
						{/if}
					</div>
				{/if}

				<!-- CONNECTORS -->
				{#if activeTab === 'connectors' && $isAdmin}
					<div class="tab-content" transition:fade={{ duration: 150 }}>
						<ConnectorManager />
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

	/* Alerts & Messages */
</style>
