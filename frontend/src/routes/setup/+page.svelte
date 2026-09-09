<script lang="ts">
	import { goto } from '$app/navigation';
	import { onMount, onDestroy } from 'svelte';
	import { auth, anyCredential } from '$lib/stores/auth.svelte';
	import {
		getWorkspaceConfig,
		setupInstallation,
		setWorkspaceConfig,
		getClaudeAuthStatus,
		startClaudeLogin,
		completeClaudeLogin,
		pollClaudeLogin,
		getGithubAuthStatus,
		startGithubWebLogin,
		loginGithubWithToken,
		completeOnboarding
	} from '$lib/api/auth';
	import { api, getErrorMessage, type WorkspaceConfig } from '$lib/api/client';
	import {
		cancelEnrollment,
		createNetwork,
		createRecoveryKey,
		getNetworkStatus,
		startEnrollment,
		unlockWithRecoveryKey,
		type CreatedRecoveryKey,
		type EnrollStarted,
		type NetworkStatus
	} from '$lib/api/network';
	import { isCompleteFingerprint, parseJoinLink } from '$lib/api/network-status';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import AddProviderModal from '$lib/components/deck/cards/settings/providers/AddProviderModal.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { Check, ChevronRight, Terminal, Github, FolderOpen, Copy, ExternalLink, Loader2, Plus, Network, Users, Smartphone, KeyRound, ShieldCheck, AlertTriangle } from 'lucide-svelte';

	// ── Step management ──────────────────────────────────────────────────
	type SetupStep = 'network' | 'workspace' | 'cli-auth' | 'providers';

	let currentStep = $state<SetupStep>('workspace');
	let completedSteps = $state(new Set<SetupStep>());
	let loadingConfig = $state(true);
	let setupError = $state('');

	// Workspace config
	let workspaceConfig = $state<WorkspaceConfig | null>(null);
	let isLocalMode = $derived(workspaceConfig?.is_local_mode ?? false);

	let steps = $derived.by(() => {
		const s: { id: SetupStep; label: string }[] = [];
		// Existing installs are already enrolled — the network step is skipped for them.
		if (networkStepNeeded) s.push({ id: 'network', label: 'Network' });
		s.push({ id: 'workspace', label: 'Workspace' });
		s.push({ id: 'cli-auth', label: 'Connect' });
		s.push({ id: 'providers', label: 'Providers' });
		return s;
	});

	let currentStepIndex = $derived(steps.findIndex(s => s.id === currentStep));

	// ── Providers step state ─────────────────────────────────────────────
	let addProviderOpen = $state(false);

	// PA10: closing the add-provider modal may have added the install's first
	// credential — refresh the auth status so $anyCredential (and the Connect
	// step's continue gate) updates immediately.
	function closeAddProvider() {
		addProviderOpen = false;
		auth.checkAuth().catch(() => {});
		providers.refreshProviders().catch(() => {});
	}

	// ── Network step state ───────────────────────────────────────────────
	// First-run choice: found a new worker network here, join one from another
	// worker, or sign in to the Shuffle account (which enrolls this worker once
	// another worker approves it — or immediately with the recovery key).
	type NetworkChoice = 'create' | 'join' | 'account';
	type HubConnection = { authorization_url?: string | null; signed_in: boolean; status: string; code: string | null; qr_svg: string | null; error: string | null };
	const REMAINING_LOCAL_STEPS = [
		'Workspace folder — where checkouts live on this machine',
		'Connect — Claude Code and GitHub logins are per machine',
		'Providers — review the provider keys synced from the network'
	];

	let networkStepNeeded = $state(false);
	let networkStatus = $state<NetworkStatus | null>(null);
	let networkChoice = $state<NetworkChoice | null>(null);
	let networkError = $state('');
	let networkBusy = $state(false);
	let networkPoll: ReturnType<typeof setInterval> | null = null;
	/** Set once this worker is enrolled through the join/account path — shows the "what remains" panel. */
	let joinedNetworkName = $state<string | null>(null);

	// create
	let newNetworkName = $state('');
	let createdRecoveryKey = $state<CreatedRecoveryKey | null>(null);
	let recoveryKeySaved = $state(false);
	let recoveryKeyCopied = $state(false);

	// join
	let joinInput = $state('');
	let joinEndpoint = $state('');
	let joinCode = $state('');
	let joinFingerprint = $state('');
	let joinCompared = $state(false);
	let joinStarted = $state<EnrollStarted | null>(null);
	let joinParsed = $derived(parseJoinLink(joinInput));
	let joinReady = $derived(
		joinParsed
			? joinCompared && isCompleteFingerprint(joinParsed.fingerprint)
			: Boolean(joinEndpoint.trim() && joinCode.trim()) && isCompleteFingerprint(joinFingerprint) && joinCompared
	);
	let enrollmentPending = $derived(networkStatus !== null && !networkStatus.enrolled && networkStatus.enrollment?.status === 'pending');

	// account
	let hubConnection = $state<HubConnection | null>(null);
	let hubWorkerName = $state('My worker');
	let hubCodeCopied = $state(false);
	let showRecoveryUnlock = $state(false);
	let recoveryKeyInput = $state('');
	let hubLinked = $derived(Boolean(networkStatus?.hub?.linked || hubConnection?.signed_in || hubConnection?.status === 'connected'));

	function stopNetworkPoll() {
		if (networkPoll) clearInterval(networkPoll);
		networkPoll = null;
	}

	function startNetworkPoll() {
		stopNetworkPoll();
		networkPoll = setInterval(refreshNetwork, 3000);
	}

	async function refreshNetwork() {
		try {
			const status = await getNetworkStatus();
			networkStatus = status;
			if (status.enrolled && !joinedNetworkName && networkChoice !== 'create') {
				joinedNetworkName = status.network?.name ?? 'your network';
				stopNetworkPoll();
			} else if (!status.enrolled && status.enrollment && status.enrollment.status !== 'pending' && status.enrollment.status !== 'complete') {
				networkError = `The join ${status.enrollment.status}${status.enrollment.error ? `: ${status.enrollment.error}` : ''}.`;
				joinStarted = null;
			}
		} catch { /* keep polling; the next poll retries */ }
		if (networkChoice === 'account' && !joinedNetworkName) {
			try { hubConnection = await api.get<HubConnection>('/mobile/hub/connection'); } catch { /* non-critical */ }
		}
	}

	function chooseNetwork(choice: NetworkChoice) {
		networkChoice = choice;
		networkError = '';
		if (choice === 'join' || choice === 'account') {
			void refreshNetwork();
			startNetworkPoll();
		} else {
			stopNetworkPoll();
		}
	}

	function backToChoices() {
		networkChoice = null;
		networkError = '';
		stopNetworkPoll();
	}

	async function createNetworkAndKey() {
		networkBusy = true;
		networkError = '';
		try {
			await createNetwork({ name: newNetworkName });
			// The recovery key is shown ONCE, right after creation — never stored.
			createdRecoveryKey = await createRecoveryKey();
			recoveryKeySaved = false;
			await refreshNetwork();
		} catch (e: unknown) {
			networkError = getErrorMessage(e, 'Failed to create the network');
		} finally {
			networkBusy = false;
		}
	}

	async function copyRecoveryKey() {
		try {
			await navigator.clipboard.writeText(createdRecoveryKey?.recovery_key ?? '');
			recoveryKeyCopied = true;
			setTimeout(() => (recoveryKeyCopied = false), 2000);
		} catch { /* clipboard may be unavailable — the key is selectable */ }
	}

	async function startJoin() {
		networkBusy = true;
		networkError = '';
		try {
			joinStarted = joinParsed
				? await startEnrollment({ link: joinInput })
				: await startEnrollment({ endpoint_url: joinEndpoint, code: joinCode, inviter_fingerprint: joinFingerprint });
			await refreshNetwork();
			startNetworkPoll();
		} catch (e: unknown) {
			networkError = getErrorMessage(e, 'Failed to start joining');
		} finally {
			networkBusy = false;
		}
	}

	async function cancelJoin() {
		networkBusy = true;
		networkError = '';
		try {
			await cancelEnrollment();
			joinStarted = null;
			joinCompared = false;
			await refreshNetwork();
		} catch (e: unknown) {
			networkError = getErrorMessage(e, 'Failed to cancel the join');
		} finally {
			networkBusy = false;
		}
	}

	async function connectAccount() {
		networkBusy = true;
		networkError = '';
		hubCodeCopied = false;
		try {
			hubConnection = await api.post<HubConnection>('/mobile/hub/connection', { name: hubWorkerName.trim() });
		} catch (e: unknown) {
			networkError = getErrorMessage(e, 'Failed to start the account sign-in');
		} finally {
			networkBusy = false;
		}
	}

	async function copyHubCode() {
		try {
			await navigator.clipboard.writeText(hubConnection?.code ?? '');
			hubCodeCopied = true;
			setTimeout(() => (hubCodeCopied = false), 2000);
		} catch { /* the code is selectable */ }
	}

	async function unlockWithRecovery() {
		networkBusy = true;
		networkError = '';
		try {
			const result = await unlockWithRecoveryKey(recoveryKeyInput, hubWorkerName.trim() || undefined);
			recoveryKeyInput = '';
			joinedNetworkName = result.network?.name ?? 'your network';
			stopNetworkPoll();
			await refreshNetwork();
		} catch (e: unknown) {
			networkError = getErrorMessage(e, 'The recovery key was not accepted');
		} finally {
			networkBusy = false;
		}
	}

	function finishNetworkStep() {
		stopNetworkPoll();
		completedSteps.add('network');
		advanceStep();
	}

	// ── Workspace state ──────────────────────────────────────────────────
	let workspacePath = $state('');
	let workspaceError = $state('');
	let savingWorkspace = $state(false);

	// ── Claude auth state ────────────────────────────────────────────────
	let claudeIsAuthenticated = $state(false);
	let claudeLoading = $state(false);
	let claudeOAuthUrl = $state('');
	let claudeOAuthState = $state('');
	let claudeAuthCode = $state('');
	let claudeCompletingLogin = $state(false);
	let claudeError = $state('');
	let claudePollInterval = $state<ReturnType<typeof setInterval> | null>(null);

	// PA10: the Connect step no longer HARD-requires the Claude login. Continuing
	// is allowed as soon as the install has any usable credential — the Claude
	// subscription just connected here, or an already-configured model provider
	// (reported by the auth status as has_any_credential).
	let canLeaveCliAuth = $derived(claudeIsAuthenticated || $anyCredential);

	// ── GitHub auth state ────────────────────────────────────────────────
	let githubIsAuthenticated = $state(false);
	let githubUser = $state<string | null>(null);
	let githubAuthMethod = $state<'device' | 'pat'>('device');
	let githubDeviceCode = $state('');
	let githubDeviceLoading = $state(false);
	let githubPAT = $state('');
	let githubPATLoading = $state(false);
	let githubError = $state('');
	let githubPollInterval = $state<ReturnType<typeof setInterval> | null>(null);
	let codeCopied = $state(false);

	// ── Lifecycle ────────────────────────────────────────────────────────
	onMount(async () => {
		try {
			workspaceConfig = await getWorkspaceConfig();
			workspacePath = workspaceConfig.workspace_path || workspaceConfig.default_path;
		} catch (e) {
			console.error('Failed to load workspace config:', e);
		}
		// Network first: a fresh worker chooses how it joins; an enrolled install
		// (or a backend without the network API) goes straight to the workspace.
		try {
			networkStatus = await getNetworkStatus();
			networkStepNeeded = !networkStatus.enrolled;
			if (networkStepNeeded) {
				currentStep = 'network';
				if (networkStatus.enrollment?.status === 'pending') chooseNetwork('join');
			}
		} catch {
			networkStepNeeded = false;
		}
		loadingConfig = false;
	});

	onDestroy(() => {
		stopNetworkPoll();
		if (claudePollInterval) clearInterval(claudePollInterval);
		if (githubPollInterval) clearInterval(githubPollInterval);
	});

	// ── Workspace step ───────────────────────────────────────────────────
	function handleDirectorySelect(path: string) {
		workspacePath = path;
		workspaceError = '';
	}

	async function saveWorkspaceAndContinue() {
		if (!workspacePath.trim()) {
			workspaceError = 'Please select or enter a workspace path';
			return;
		}

		savingWorkspace = true;
		workspaceError = '';

		try {
			await setWorkspaceConfig(workspacePath);
			if (auth.current.status?.setup_required) await setupInstallation();
			await auth.checkAuth();
			completedSteps.add('workspace');
			advanceStep();
		} catch (e: any) {
			workspaceError = e.detail || 'Failed to save workspace configuration';
		} finally {
			savingWorkspace = false;
		}
	}

	// ── Claude auth ──────────────────────────────────────────────────────
	async function loadCliAuthStatus() {
		try {
			const claudeStatus = await getClaudeAuthStatus();
			claudeIsAuthenticated = claudeStatus.authenticated;
		} catch { /* non-critical */ }

		// Refresh the auth status so has_any_credential reflects providers that
		// were configured before this run of the wizard (PA10 gate below).
		try {
			await auth.checkAuth();
		} catch { /* non-critical */ }

		try {
			const githubStatus = await getGithubAuthStatus();
			githubIsAuthenticated = githubStatus.authenticated;
			githubUser = githubStatus.user;
		} catch { /* non-critical */ }
	}

	async function startClaudeAuth() {
		claudeLoading = true;
		claudeError = '';

		try {
			const result = await startClaudeLogin();
			if (result.already_authenticated) {
				claudeIsAuthenticated = true;
				return;
			}
			if (result.success && result.oauth_url && result.state) {
				claudeOAuthUrl = result.oauth_url;
				claudeOAuthState = result.state;
				// Start polling for auto-detection
				startClaudePoll();
			} else {
				claudeError = result.error || 'Failed to start Claude authentication';
			}
		} catch (e: any) {
			claudeError = e.detail || 'Failed to start Claude authentication';
		} finally {
			claudeLoading = false;
		}
	}

	function startClaudePoll() {
		if (claudePollInterval) clearInterval(claudePollInterval);
		claudePollInterval = setInterval(async () => {
			try {
				const result = await pollClaudeLogin();
				if (result.authenticated) {
					claudeIsAuthenticated = true;
					claudeOAuthUrl = '';
					claudeAuthCode = '';
					if (claudePollInterval) clearInterval(claudePollInterval);
				}
			} catch { /* ignore polling errors */ }
		}, 3000);
	}

	async function submitClaudeCode() {
		if (!claudeAuthCode.trim() || !claudeOAuthState) return;
		claudeCompletingLogin = true;
		claudeError = '';

		try {
			const result = await completeClaudeLogin(claudeAuthCode.trim(), claudeOAuthState);
			if (result.success) {
				claudeIsAuthenticated = true;
				claudeOAuthUrl = '';
				claudeAuthCode = '';
				if (claudePollInterval) clearInterval(claudePollInterval);
			} else {
				claudeError = result.error || 'Failed to complete authentication';
			}
		} catch (e: any) {
			claudeError = e.detail || 'Failed to complete authentication';
		} finally {
			claudeCompletingLogin = false;
		}
	}

	// ── GitHub auth ──────────────────────────────────────────────────────
	async function startGithubDeviceFlow() {
		githubDeviceLoading = true;
		githubError = '';

		try {
			const result = await startGithubWebLogin();
			if (result.success && result.user_code) {
				githubDeviceCode = result.user_code;
				startGithubPoll();
			} else {
				githubError = result.error || 'Failed to start device flow';
			}
		} catch (e: any) {
			githubError = e.detail || 'Failed to start device flow';
		} finally {
			githubDeviceLoading = false;
		}
	}

	function startGithubPoll() {
		if (githubPollInterval) clearInterval(githubPollInterval);
		githubPollInterval = setInterval(async () => {
			try {
				const status = await getGithubAuthStatus();
				if (status.authenticated) {
					githubIsAuthenticated = true;
					githubUser = status.user;
					githubDeviceCode = '';
					if (githubPollInterval) clearInterval(githubPollInterval);
				}
			} catch { /* ignore polling errors */ }
		}, 3000);
	}

	async function connectGithubPAT() {
		if (!githubPAT.trim()) return;
		githubPATLoading = true;
		githubError = '';

		try {
			const result = await loginGithubWithToken(githubPAT.trim());
			if (result.success) {
				githubIsAuthenticated = true;
				githubPAT = '';
				// Fetch username
				try {
					const status = await getGithubAuthStatus();
					githubUser = status.user;
				} catch { /* non-critical */ }
			} else {
				githubError = result.error || 'Failed to authenticate with token';
			}
		} catch (e: any) {
			githubError = e.detail || 'Failed to authenticate with token';
		} finally {
			githubPATLoading = false;
		}
	}

	async function copyDeviceCode() {
		try {
			await navigator.clipboard.writeText(githubDeviceCode);
			codeCopied = true;
			setTimeout(() => codeCopied = false, 2000);
		} catch { /* clipboard API may not be available */ }
	}

	// ── Step navigation ──────────────────────────────────────────────────
	function advanceStep() {
		const idx = steps.findIndex(s => s.id === currentStep);
		if (idx < steps.length - 1) {
			const nextStep = steps[idx + 1].id;
			currentStep = nextStep;
			if (nextStep === 'cli-auth') {
				loadCliAuthStatus();
				providers.ensureLoaded();
			}
			if (nextStep === 'providers') {
				providers.ensureLoaded();
			}
		}
	}

	function goBack() {
		const idx = steps.findIndex(s => s.id === currentStep);
		if (idx > 0) {
			currentStep = steps[idx - 1].id;
		}
	}

	async function finishSetup() {
		try {
			await completeOnboarding();
			await auth.checkAuth();
			goto('/');
		} catch {
			setupError = 'Setup could not be saved. Please try again.';
		}
	}
</script>

<svelte:head>
	<title>Setup - AI Shuffle</title>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
</svelte:head>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card p-6 sm:p-8 w-full transition-all duration-300 {currentStep === 'workspace' || currentStep === 'network' ? 'max-w-xl' : currentStep === 'cli-auth' || currentStep === 'providers' ? 'max-w-lg' : 'max-w-md'}">
		{#if loadingConfig}
			<div class="text-center py-8">
				<Loader2 size={32} strokeWidth={1.5} class="mx-auto mb-4 animate-spin text-muted-foreground" />
				<p class="text-muted-foreground">Loading...</p>
			</div>
		{:else}
			{#if setupError}<p role="alert" class="text-destructive mb-4">{setupError}</p>{/if}
			<!-- Header -->
			<div class="text-center mb-6 sm:mb-8">
				<h1 class="text-xl sm:text-2xl font-bold text-foreground mb-2">Welcome to AI Shuffle</h1>
				<p class="text-muted-foreground text-sm sm:text-base">
					{#if currentStep === 'network'}
						Choose how this worker joins your setup — settings, profiles and keys sync across your workers
					{:else if currentStep === 'workspace'}
						Choose where project checkouts will be created on this machine
					{:else if currentStep === 'cli-auth'}
						Connect your development tools
					{:else}
						Optional — connect OpenAI, OpenRouter, or more Claude accounts. You can always do this later in Settings → Providers.
					{/if}
				</p>
			</div>

			<!-- Step indicator -->
			<div class="flex items-center justify-center gap-1 mb-6">
				{#each steps as step, i}
					<div class="flex items-center gap-1">
						<div class="flex items-center gap-1.5">
							<div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold transition-colors
								{completedSteps.has(step.id) ? 'bg-success text-success-foreground' :
								 currentStep === step.id ? 'bg-primary text-primary-foreground' :
								 'bg-muted text-muted-foreground'}">
								{#if completedSteps.has(step.id)}
									<Check size={14} strokeWidth={2.5} />
								{:else}
									{i + 1}
								{/if}
							</div>
							<span class="text-xs font-medium hidden sm:inline
								{currentStep === step.id ? 'text-foreground' : 'text-muted-foreground'}">
								{step.label}
							</span>
						</div>
						{#if i < steps.length - 1}
							<div class="w-6 sm:w-10 h-px mx-1
								{completedSteps.has(step.id) ? 'bg-success' : 'bg-border'}"></div>
						{/if}
					</div>
				{/each}
			</div>

			{#if currentStep === 'network'}
				<div class="space-y-4">
					{#if createdRecoveryKey}
						<!-- Shown ONCE right after creating the network. -->
						<div class="bg-warning/10 border border-warning/40 rounded-lg p-4 space-y-3">
							<div class="flex items-start gap-3">
								<KeyRound size={20} strokeWidth={1.5} class="text-warning mt-0.5 shrink-0" />
								<div>
									<h3 class="text-foreground font-medium mb-1">Save your recovery key</h3>
									<p class="text-muted-foreground text-sm">It restores the network onto a fresh worker when no other worker of yours is online. It will <strong>not</strong> be shown again.</p>
								</div>
							</div>
							<div class="flex items-center gap-2">
								<code class="flex-1 text-sm font-mono font-bold tracking-wider text-foreground bg-muted/60 border border-border rounded-lg px-3 py-2 select-all break-all">{createdRecoveryKey.recovery_key}</code>
								<button type="button" onclick={copyRecoveryKey} class="btn btn-secondary px-3 py-2 text-xs flex items-center gap-1 shrink-0">
									{#if recoveryKeyCopied}<Check size={12} strokeWidth={2} /> Copied{:else}<Copy size={12} strokeWidth={1.5} /> Copy{/if}
								</button>
							</div>
							<label class="flex items-start gap-2 text-sm text-foreground cursor-pointer">
								<input type="checkbox" bind:checked={recoveryKeySaved} class="mt-0.5" />
								<span>I saved this key somewhere safe.</span>
							</label>
						</div>
						<button type="button" onclick={finishNetworkStep} class="btn btn-primary w-full py-3" disabled={!recoveryKeySaved}>Continue</button>
					{:else if joinedNetworkName}
						<!-- Enrolled through join / account / recovery: say what still needs local setup. -->
						<div class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2">
							<Check size={16} strokeWidth={2} />
							<span>Joined <strong>{joinedNetworkName}</strong>. Your settings, profiles and provider keys sync from the network.</span>
						</div>
						<div class="bg-muted/30 border border-border rounded-lg p-4 space-y-2">
							<p class="text-sm font-medium text-foreground">The remaining steps set up this machine only:</p>
							<ul class="text-sm text-muted-foreground list-disc pl-5 space-y-1">
								{#each REMAINING_LOCAL_STEPS as step}<li>{step}</li>{/each}
							</ul>
						</div>
						<button type="button" onclick={finishNetworkStep} class="btn btn-primary w-full py-3">Continue</button>
					{:else if networkChoice === null}
						<div class="grid gap-3">
							<button type="button" onclick={() => chooseNetwork('create')} class="text-left bg-muted/30 hover:bg-primary/10 border border-border hover:border-primary/40 rounded-lg p-4 transition-colors">
								<div class="flex items-start gap-3">
									<Network size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
									<div>
										<h3 class="text-foreground font-medium mb-1">Start a new worker</h3>
										<p class="text-muted-foreground text-sm">This is your first worker. It founds a new network; you can invite other workers later.</p>
									</div>
								</div>
							</button>
							<button type="button" onclick={() => chooseNetwork('join')} class="text-left bg-muted/30 hover:bg-primary/10 border border-border hover:border-primary/40 rounded-lg p-4 transition-colors">
								<div class="flex items-start gap-3">
									<Users size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
									<div>
										<h3 class="text-foreground font-medium mb-1">Join from another worker</h3>
										<p class="text-muted-foreground text-sm">Another worker of yours is on the same network — use its invite link or code and compare fingerprints.</p>
									</div>
								</div>
							</button>
							<button type="button" onclick={() => chooseNetwork('account')} class="text-left bg-muted/30 hover:bg-primary/10 border border-border hover:border-primary/40 rounded-lg p-4 transition-colors">
								<div class="flex items-start gap-3">
									<Smartphone size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
									<div>
										<h3 class="text-foreground font-medium mb-1">Sign in to Shuffle</h3>
										<p class="text-muted-foreground text-sm">Link this worker to your account. One of your online workers approves it, or your recovery key restores the network here.</p>
									</div>
								</div>
							</button>
						</div>
					{:else if networkChoice === 'create'}
						<div class="space-y-3">
							<label for="networkName" class="block text-xs text-muted-foreground">Network name (optional)</label>
							<input type="text" id="networkName" bind:value={newNetworkName} maxlength="100" class="input text-sm" placeholder="Home" />
							<p class="text-xs text-muted-foreground">A recovery key is created right after the network and shown once.</p>
						</div>
						<div class="flex gap-3">
							<button type="button" onclick={backToChoices} class="btn btn-secondary flex-1 py-3" disabled={networkBusy}>Back</button>
							<button type="button" onclick={createNetworkAndKey} class="btn btn-primary flex-1 py-3" disabled={networkBusy}>
								{#if networkBusy}<Loader2 size={16} strokeWidth={2} class="inline-block animate-spin mr-2" />{/if}
								Create network
							</button>
						</div>
					{:else if networkChoice === 'join'}
						{#if enrollmentPending}
							<div class="space-y-3 bg-muted/30 border border-border rounded-lg p-4">
								<div class="flex items-center gap-2 text-sm text-foreground font-medium">
									<Loader2 size={14} strokeWidth={2} class="animate-spin" />
									Waiting for approval on {joinStarted?.inviter.name || networkStatus?.enrollment?.endpoint_url || 'the inviting worker'}
								</div>
								<p class="text-sm text-muted-foreground">On that worker, open Settings → Devices → Pending approvals and compare this fingerprint before approving:</p>
								<code class="block text-sm font-mono font-bold tracking-wider text-foreground bg-muted/60 border border-border rounded-lg px-3 py-2 select-all break-all">{networkStatus?.enrollment?.fingerprint || joinStarted?.your_fingerprint || networkStatus?.device.fingerprint}</code>
								{#if joinStarted?.inviter.network_name}<p class="text-xs text-muted-foreground">Network: {joinStarted.inviter.network_name}</p>{/if}
							</div>
							<div class="flex gap-3">
								<button type="button" onclick={cancelJoin} class="btn btn-secondary flex-1 py-3" disabled={networkBusy}>Cancel join</button>
							</div>
						{:else}
							<div class="space-y-3">
								<p class="text-sm text-muted-foreground">On the other worker, open Settings → Devices → Invite another worker. Paste its link, or type the code and address. You must also read the inviter's fingerprint from that screen.</p>
								<div>
									<label for="joinLink" class="block text-xs text-muted-foreground mb-1">Join link</label>
									<input type="text" id="joinLink" bind:value={joinInput} class="input text-sm font-mono" placeholder="shuffle://network/join?endpoint=…&code=…&fp=…" autocomplete="off" spellcheck="false" />
								</div>
								{#if joinParsed}
									<div class="bg-muted/30 border border-border rounded-lg p-3 text-sm space-y-1">
										<div class="flex gap-2"><span class="text-muted-foreground w-32 shrink-0">Address</span><code class="text-xs break-all">{joinParsed.endpoint || '—'}</code></div>
										<div class="flex gap-2"><span class="text-muted-foreground w-32 shrink-0">Code</span><code class="text-xs">{joinParsed.code || '—'}</code></div>
										<div class="flex gap-2"><span class="text-muted-foreground w-32 shrink-0">Inviter fingerprint</span><code class="text-xs break-all">{joinParsed.fingerprint || '—'}</code></div>
									</div>
								{:else}
									<div class="grid sm:grid-cols-2 gap-3">
										<div>
											<label for="joinEndpoint" class="block text-xs text-muted-foreground mb-1">Worker address</label>
											<input type="text" id="joinEndpoint" bind:value={joinEndpoint} class="input text-sm" placeholder="https://10.0.0.5:8000" autocomplete="off" spellcheck="false" />
										</div>
										<div>
											<label for="joinCode" class="block text-xs text-muted-foreground mb-1">Join code</label>
											<input type="text" id="joinCode" bind:value={joinCode} class="input text-sm font-mono" placeholder="ABCD-EFGH-JKLM" autocomplete="off" spellcheck="false" />
										</div>
										<div class="sm:col-span-2">
											<label for="joinFingerprint" class="block text-xs text-muted-foreground mb-1">Inviter fingerprint (read it on the other screen)</label>
											<input type="text" id="joinFingerprint" bind:value={joinFingerprint} class="input text-sm font-mono" placeholder="xxxx-xxxx-xxxx-xxxx-xxxx-xxxx-xxxx-xxxx" autocomplete="off" spellcheck="false" />
										</div>
									</div>
								{/if}
								<label class="flex items-start gap-2 text-sm text-foreground cursor-pointer">
									<input type="checkbox" bind:checked={joinCompared} class="mt-0.5" />
									<span>I compared the inviter's fingerprint with the one shown on the other worker.</span>
								</label>
							</div>
							<div class="flex gap-3">
								<button type="button" onclick={backToChoices} class="btn btn-secondary flex-1 py-3" disabled={networkBusy}>Back</button>
								<button type="button" onclick={startJoin} class="btn btn-primary flex-1 py-3" disabled={networkBusy || !joinReady}>
									{#if networkBusy}<Loader2 size={16} strokeWidth={2} class="inline-block animate-spin mr-2" />{/if}
									Join network
								</button>
							</div>
						{/if}
					{:else if networkChoice === 'account'}
						<div class="space-y-3">
							{#if hubLinked}
								<div class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2">
									<Check size={16} strokeWidth={2} />
									<span>Linked to your account</span>
								</div>
								<div class="space-y-3 bg-muted/30 border border-border rounded-lg p-4">
									<div class="flex items-center gap-2 text-sm text-foreground font-medium">
										<Loader2 size={14} strokeWidth={2} class="animate-spin" />
										Waiting for one of your workers to approve this device
									</div>
									<p class="text-sm text-muted-foreground">On an online worker, open Settings → Devices → Pending approvals and compare this fingerprint:</p>
									<code class="block text-sm font-mono font-bold tracking-wider text-foreground bg-muted/60 border border-border rounded-lg px-3 py-2 select-all break-all">{networkStatus?.device.fingerprint ?? '…'}</code>
								</div>
								{#if showRecoveryUnlock}
									<div class="space-y-2 bg-muted/30 border border-border rounded-lg p-4">
										<p class="text-sm text-foreground font-medium">Enter your recovery key</p>
										<p class="text-xs text-muted-foreground">Restores the account's network onto this worker without another worker approving it.</p>
										<div class="flex gap-2">
											<input type="text" bind:value={recoveryKeyInput} class="input flex-1 text-sm font-mono" placeholder="A1-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-X" autocomplete="off" spellcheck="false" />
											<button type="button" onclick={unlockWithRecovery} class="btn btn-primary px-4" disabled={networkBusy || recoveryKeyInput.trim().length < 20}>
												{#if networkBusy}<Loader2 size={14} strokeWidth={2} class="animate-spin" />{:else}Restore{/if}
											</button>
										</div>
									</div>
								{:else}
									<button type="button" onclick={() => (showRecoveryUnlock = true)} class="btn btn-secondary w-full py-2.5 text-sm flex items-center justify-center gap-2">
										<KeyRound size={16} strokeWidth={1.5} />
										No worker online to approve? Enter recovery key
									</button>
								{/if}
							{:else if hubConnection?.code}
								<div class="space-y-3 bg-muted/30 border border-border rounded-lg p-4">
									{#if hubConnection.authorization_url}
										<a href={hubConnection.authorization_url} target="_blank" rel="noopener noreferrer" class="btn btn-primary w-full py-2 text-sm flex items-center justify-center gap-2">
											<ExternalLink size={14} strokeWidth={1.5} />
											Sign in and link this worker
										</a>
										<p class="text-xs text-muted-foreground text-center">Return here after approving this worker — or scan the code with the Shuffle app.</p>
									{:else}
										<p class="text-sm text-muted-foreground">In the Shuffle app, sign in, choose <strong>Add worker</strong>, then scan this code.</p>
									{/if}
									{#if hubConnection.qr_svg}<div class="w-44 max-w-full p-3 bg-white rounded-xl mx-auto [&_svg]:block [&_svg]:w-full [&_svg]:h-auto" aria-label="Worker connection QR code">{@html hubConnection.qr_svg}</div>{/if}
									<div class="flex items-center justify-center gap-3">
										<code class="text-xl font-mono font-bold tracking-widest text-foreground select-all">{hubConnection.code.match(/.{1,4}/g)?.join(' ')}</code>
										<button type="button" onclick={copyHubCode} class="btn btn-secondary px-2 py-1.5 text-xs flex items-center gap-1">
											{#if hubCodeCopied}<Check size={12} strokeWidth={2} /> Copied{:else}<Copy size={12} strokeWidth={1.5} /> Copy{/if}
										</button>
									</div>
									<div class="flex items-center justify-center gap-2 text-xs text-muted-foreground">
										<Loader2 size={12} strokeWidth={2} class="animate-spin" />
										Waiting for the account link… the code expires after ten minutes.
									</div>
								</div>
							{:else if hubConnection?.status === 'locked' || hubConnection?.status === 'unavailable'}
								<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">Remote access is unavailable until the issue below is resolved.</div>
							{:else}
								<div>
									<label for="hubWorkerName" class="block text-xs text-muted-foreground mb-1">Worker name</label>
									<input type="text" id="hubWorkerName" bind:value={hubWorkerName} maxlength="80" class="input text-sm" placeholder="My worker" />
								</div>
								<button type="button" onclick={connectAccount} class="btn btn-primary w-full py-2.5 text-sm" disabled={networkBusy || !hubWorkerName.trim()}>
									{#if networkBusy}<Loader2 size={14} strokeWidth={2} class="inline-block animate-spin mr-2" />{/if}
									Sign in to Shuffle
								</button>
							{/if}
							{#if hubConnection?.error}
								<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{hubConnection.error}</div>
							{/if}
						</div>
						<div class="flex gap-3">
							<button type="button" onclick={backToChoices} class="btn btn-secondary flex-1 py-3" disabled={networkBusy}>Back</button>
						</div>
					{/if}

					{#if networkError}
						<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm flex items-start gap-2">
							<AlertTriangle size={14} strokeWidth={2} class="mt-0.5 shrink-0" />
							<span>{networkError}</span>
						</div>
					{/if}
					{#if networkChoice !== null && !joinedNetworkName && !createdRecoveryKey}
						<p class="text-xs text-muted-foreground text-center flex items-center justify-center gap-1"><ShieldCheck size={12} strokeWidth={1.5} /> Approve only devices you own. Fingerprints must match exactly.</p>
					{/if}
				</div>

			{:else if currentStep === 'workspace'}
				<div class="space-y-4">
					<div class="bg-primary/10 border border-primary/30 rounded-lg p-4">
						<div class="flex items-start gap-3">
							<FolderOpen size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
							<div>
								<h3 class="text-foreground font-medium mb-1">Select Workspace Folder</h3>
								<p class="text-muted-foreground text-sm">
									This is where project checkouts will be created on this machine.
								</p>
							</div>
						</div>
					</div>

					<!-- Directory browser -->
					<DirectoryBrowser
						onSelect={handleDirectorySelect}
						selectedPath={workspacePath}
					/>

					<!-- Selected path display -->
					{#if workspacePath}
						<div class="text-sm text-foreground font-mono bg-muted/50 px-3 py-2 rounded-lg border border-border flex items-center gap-2">
							<FolderOpen size={14} strokeWidth={1.5} class="text-primary shrink-0" />
							<span class="truncate">{workspacePath}</span>
						</div>
					{/if}

					<!-- Manual input fallback -->
					<div>
						<label for="workspaceManual" class="block text-xs text-muted-foreground mb-1">
							Or type a path manually
						</label>
						<input
							type="text"
							id="workspaceManual"
							bind:value={workspacePath}
							class="input font-mono text-sm"
							placeholder="Enter workspace path"
						/>
					</div>

					{#if workspaceError}
						<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">
							{workspaceError}
						</div>
					{/if}

					<div class="flex gap-3">
						<button type="button" onclick={goBack} class="btn btn-secondary flex-1 py-3">
							Back
						</button>
						<button
							type="button"
							onclick={saveWorkspaceAndContinue}
							class="btn btn-primary flex-1 py-3"
							disabled={!workspacePath.trim() || savingWorkspace}
						>
							{#if savingWorkspace}
								<Loader2 size={16} strokeWidth={2} class="inline-block animate-spin mr-2" />
							{/if}
							Continue
						</button>
					</div>
				</div>

			<!-- ═══════════════════════════════════════════════════════════ -->
			<!-- Step 3: CLI Authentication -->
			<!-- ═══════════════════════════════════════════════════════════ -->
			{:else if currentStep === 'cli-auth'}
				<div class="space-y-6">

					<!-- ── Claude Code Section ──────────────────────────── -->
					<div class="space-y-3">
						<div class="flex items-center gap-2">
							<Terminal size={16} strokeWidth={1.5} class="text-foreground" />
							<span class="text-sm font-semibold text-foreground">Claude Code</span>
							<!-- PA10: the step-level requirement is "any usable credential" (see the
							     Continue gate below) — claude connect itself is recommended, not required. -->
							<span class="text-xs px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/30 font-medium">Recommended</span>
						</div>

						{#if claudeIsAuthenticated}
							<div class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2">
								<Check size={16} strokeWidth={2} />
								<span>Claude Code is connected</span>
							</div>
						{:else if claudeOAuthUrl}
							<div class="space-y-3 bg-muted/30 border border-border rounded-lg p-4">
								<p class="text-sm text-muted-foreground">
									1. Open the link below and authenticate with your Anthropic account:
								</p>
								<a href={claudeOAuthUrl} target="_blank" rel="noopener noreferrer"
									class="btn btn-secondary w-full py-2 text-sm flex items-center justify-center gap-2">
									<ExternalLink size={14} strokeWidth={1.5} />
									Open Claude Authentication
								</a>
								<p class="text-sm text-muted-foreground">
									2. After authenticating, paste the authorization code below:
								</p>
								<div class="flex gap-2">
									<input
										type="text"
										bind:value={claudeAuthCode}
										class="input flex-1 font-mono text-sm"
										placeholder="Paste authorization code"
									/>
									<button
										type="button"
										onclick={submitClaudeCode}
										class="btn btn-primary px-4"
										disabled={!claudeAuthCode.trim() || claudeCompletingLogin}
									>
										{#if claudeCompletingLogin}
											<Loader2 size={14} strokeWidth={2} class="animate-spin" />
										{:else}
											Submit
										{/if}
									</button>
								</div>
								<p class="text-xs text-muted-foreground text-center">
									Or wait — authentication will be detected automatically.
								</p>
							</div>
						{:else}
							<button
								type="button"
								onclick={startClaudeAuth}
								class="btn btn-primary w-full py-2.5 text-sm"
								disabled={claudeLoading}
							>
								{#if claudeLoading}
									<Loader2 size={14} strokeWidth={2} class="inline-block animate-spin mr-2" />
								{/if}
								Connect Claude Code
							</button>
						{/if}

						{#if claudeError}
							<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">
								{claudeError}
							</div>
						{/if}
					</div>

					<div class="border-t border-border"></div>

					<!-- ── Model providers Section (PA10) ───────────────── -->
					<!-- On a fresh install the Providers step is unreachable until this
					     step's gate passes, so provider-only users need an entry point HERE. -->
					<div class="space-y-3">
						<div class="flex items-center gap-2">
							<Plus size={16} strokeWidth={1.5} class="text-foreground" />
							<span class="text-sm font-semibold text-foreground">Model providers</span>
						</div>
						<p class="text-xs text-muted-foreground">
							Prefer OpenAI, OpenRouter, or an API key? Add a model provider — you can finish setup without a Claude login.
						</p>

						{#if providers.current.providers.length > 0}
							<div class="space-y-2">
								{#each providers.current.providers as provider (provider.id)}
									<div class="flex items-center justify-between gap-2 bg-muted/30 border border-border rounded-lg px-3 py-2">
										<span class="text-sm text-foreground truncate">{provider.name}</span>
										<span class="text-xs text-muted-foreground shrink-0">{provider.provider_type}</span>
									</div>
								{/each}
							</div>
						{/if}

						<button
							type="button"
							onclick={() => (addProviderOpen = true)}
							class="btn btn-secondary w-full py-2.5 text-sm flex items-center justify-center gap-2"
						>
							<Plus size={16} strokeWidth={1.5} />
							Add a model provider
						</button>
					</div>

					<div class="border-t border-border"></div>

					<!-- ── GitHub Section ───────────────────────────────── -->
					<div class="space-y-3">
						<div class="flex items-center gap-2">
							<Github size={16} strokeWidth={1.5} class="text-foreground" />
							<span class="text-sm font-semibold text-foreground">GitHub</span>
							<span class="text-xs px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/30 font-medium">Recommended</span>
						</div>
						<p class="text-xs text-muted-foreground">
							Connect GitHub for pull request management, code reviews, and repository features. You can also configure this later in Settings.
						</p>

						{#if githubIsAuthenticated}
							<div class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2">
								<Check size={16} strokeWidth={2} />
								<span>GitHub is connected{githubUser ? ` as @${githubUser}` : ''}</span>
							</div>
						{:else}
							<!-- Auth method toggle -->
							<div class="flex rounded-lg border border-border overflow-hidden text-sm">
								<button
									type="button"
									class="flex-1 px-3 py-1.5 transition-colors {githubAuthMethod === 'device' ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground'}"
									onclick={() => githubAuthMethod = 'device'}
								>
									Browser Login
								</button>
								<button
									type="button"
									class="flex-1 px-3 py-1.5 transition-colors border-l border-border {githubAuthMethod === 'pat' ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground'}"
									onclick={() => githubAuthMethod = 'pat'}
								>
									Access Token
								</button>
							</div>

							{#if githubAuthMethod === 'device'}
								{#if githubDeviceCode}
									<div class="space-y-3 bg-muted/30 border border-border rounded-lg p-4">
										<p class="text-sm text-muted-foreground text-center">
											Enter this code at GitHub:
										</p>
										<div class="flex items-center justify-center gap-3">
											<code class="text-2xl font-mono font-bold tracking-widest text-foreground select-all">
												{githubDeviceCode}
											</code>
											<button
												type="button"
												onclick={copyDeviceCode}
												class="btn btn-secondary px-2 py-1.5 text-xs flex items-center gap-1"
											>
												{#if codeCopied}
													<Check size={12} strokeWidth={2} />
													Copied
												{:else}
													<Copy size={12} strokeWidth={1.5} />
													Copy
												{/if}
											</button>
										</div>
										<a
											href="https://github.com/login/device"
											target="_blank"
											rel="noopener noreferrer"
											class="btn btn-secondary w-full py-2 text-sm flex items-center justify-center gap-2"
										>
											<ExternalLink size={14} strokeWidth={1.5} />
											Open github.com/login/device
										</a>
										<div class="flex items-center justify-center gap-2 text-xs text-muted-foreground">
											<Loader2 size={12} strokeWidth={2} class="animate-spin" />
											Waiting for authentication...
										</div>
									</div>
								{:else}
									<button
										type="button"
										onclick={startGithubDeviceFlow}
										class="btn btn-secondary w-full py-2.5 text-sm"
										disabled={githubDeviceLoading}
									>
										{#if githubDeviceLoading}
											<Loader2 size={14} strokeWidth={2} class="inline-block animate-spin mr-2" />
										{/if}
										Login with Browser
									</button>
								{/if}
							{:else}
								<div class="space-y-2">
									<div class="flex gap-2">
										<input
											type="password"
											bind:value={githubPAT}
											class="input flex-1 text-sm font-mono"
											placeholder="ghp_..."
										/>
										<button
											type="button"
											onclick={connectGithubPAT}
											class="btn btn-secondary px-4"
											disabled={!githubPAT.trim() || githubPATLoading}
										>
											{#if githubPATLoading}
												<Loader2 size={14} strokeWidth={2} class="animate-spin" />
											{:else}
												Connect
											{/if}
										</button>
									</div>
									<p class="text-xs text-muted-foreground">
										Create a token at <a href="https://github.com/settings/tokens" target="_blank" rel="noopener noreferrer" class="text-primary hover:underline">github.com/settings/tokens</a> with <code class="text-xs">repo</code> scope.
									</p>
								</div>
							{/if}
						{/if}

						{#if githubError}
							<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">
								{githubError}
							</div>
						{/if}
					</div>

					<!-- ── Navigation ──────────────────────────────────── -->
					<div class="border-t border-border pt-4">
						<div class="flex gap-3">
							<button type="button" onclick={goBack} class="btn btn-secondary flex-1 py-3">
								Back
							</button>
							<button
								type="button"
								onclick={() => { completedSteps.add('cli-auth'); advanceStep(); }}
								class="btn btn-primary flex-1 py-3"
								disabled={!canLeaveCliAuth}
							>
								{canLeaveCliAuth ? 'Continue' : 'Connect a Model Provider to Continue'}
							</button>
						</div>
						{#if !canLeaveCliAuth}
							<p class="text-xs text-muted-foreground text-center mt-2">
								Connect Claude Code or configure a model provider to continue.
							</p>
						{/if}
					</div>
				</div>

			<!-- ═══════════════════════════════════════════════════════════ -->
			<!-- Step 4: Providers (optional) -->
			<!-- ═══════════════════════════════════════════════════════════ -->
			{:else if currentStep === 'providers'}
				<div class="space-y-6">

					<!-- ── Add providers Section ────────────────────────── -->
					<div class="space-y-3">
						<div class="flex items-center gap-2">
							<Plus size={16} strokeWidth={1.5} class="text-foreground" />
							<span class="text-sm font-semibold text-foreground">Add AI providers</span>
							<span class="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground border border-border font-medium">Optional</span>
						</div>

						<!-- Configured-providers summary -->
						{#if providers.current.providers.length === 0}
							<p class="text-sm text-muted-foreground">
								You can add more model providers now or later in Settings.
							</p>
						{:else}
							<div class="space-y-2">
								{#each providers.current.providers as provider (provider.id)}
									<div class="flex items-center justify-between gap-2 bg-muted/30 border border-border rounded-lg px-3 py-2">
										<span class="text-sm text-foreground truncate">{provider.name}</span>
										<span class="text-xs text-muted-foreground shrink-0">{provider.provider_type}</span>
									</div>
								{/each}
							</div>
						{/if}

						<button
							type="button"
							onclick={() => (addProviderOpen = true)}
							class="btn btn-secondary w-full py-2.5 text-sm flex items-center justify-center gap-2"
						>
							<Plus size={16} strokeWidth={1.5} />
							Add provider
						</button>
					</div>

					<!-- ── Navigation ──────────────────────────────────── -->
					<div class="border-t border-border pt-4">
						<div class="flex gap-3">
							<button type="button" onclick={goBack} class="btn btn-secondary flex-1 py-3">
								Back
							</button>
							<button type="button" onclick={finishSetup} class="btn btn-primary flex-1 py-3">
								Finish Setup
							</button>
						</div>
					</div>
				</div>
			{/if}

			{#if addProviderOpen}
				<AddProviderModal onClose={closeAddProvider} />
			{/if}
		{/if}
	</div>
</div>
