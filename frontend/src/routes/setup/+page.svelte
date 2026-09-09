<script lang="ts">
	/**
	 * First-run setup for a worker (the desktop app and Docker both land here).
	 *
	 * 1. Start — a clean install, sign in to a Shuffle account to sync settings
	 *    from your other workers, or join another worker with its invite.
	 * 2. Workspace — where project checkouts live on this machine.
	 * 3. Providers & GitHub — model providers and the GitHub login used for
	 *    repository features. Both are optional and live in Settings later.
	 *
	 * Nothing here waits for an approval on another device: a clean install is
	 * immediate, an invite admits this worker on contact, and an account admits
	 * it as soon as one of your other workers is online.
	 */
	import { goto } from '$app/navigation';
	import { onMount, onDestroy } from 'svelte';
	import { auth, anyCredential } from '$lib/stores/auth.svelte';
	import {
		getWorkspaceConfig, setupInstallation, setWorkspaceConfig,
		completeOnboarding
	} from '$lib/api/auth';
	import { getErrorMessage, type WorkspaceConfig } from '$lib/api/client';
	import { createNetwork, getNetworkStatus, unlockWithRecoveryKey, type NetworkStatus } from '$lib/api/network';
	import SetupProgress from '$lib/components/SetupProgress.svelte';
	import GitHubConnection from '$lib/components/GitHubConnection.svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';
	import AddProviderModal from '$lib/components/deck/cards/settings/providers/AddProviderModal.svelte';
	import AccountPanel from '$lib/components/deck/cards/settings/AccountPanel.svelte';
	import JoinNetworkPanel from '$lib/components/deck/cards/settings/JoinNetworkPanel.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { Check, FolderOpen, Loader2, Plus, Sparkles, UserRound, Link2, KeyRound, AlertTriangle, ChevronLeft } from 'lucide-svelte';

	// ── Steps ────────────────────────────────────────────────────────────────
	type SetupStep = 'start' | 'workspace' | 'providers';
	let currentStep = $state<SetupStep>('workspace');
	let completedSteps = $state(new Set<SetupStep>());
	let loadingConfig = $state(true);
	let setupError = $state('');
	let startStepNeeded = $state(false);
	let steps = $derived.by(() => {
		const s: { id: SetupStep; label: string }[] = [];
		if (startStepNeeded) s.push({ id: 'start', label: 'Start' });
		s.push({ id: 'workspace', label: 'Workspace' });
		s.push({ id: 'providers', label: 'Providers & GitHub' });
		return s;
	});

	// ── Start step ───────────────────────────────────────────────────────────
	type StartChoice = 'clean' | 'account' | 'invite';
	let choice = $state<StartChoice | null>(null);
	let networkStatus = $state<NetworkStatus | null>(null);
	let networkBusy = $state(false);
	let networkError = $state('');
	let workerName = $state('My worker');
	let recoveryKeyInput = $state('');
	let showRecovery = $state(false);
	let networkPoll: ReturnType<typeof setInterval> | null = null;

	let workspaceConfig = $state<WorkspaceConfig | null>(null);
	const enrolled = $derived(Boolean(networkStatus?.enrolled));
	const syncing = $derived(enrolled && networkStatus?.network?.bootstrap_state === 'syncing');
	const ready = $derived(enrolled && !syncing);
	const linked = $derived(Boolean(networkStatus?.hub?.linked));
	const waitingOnAccount = $derived(!enrolled && linked && networkStatus?.hub?.status === 'pending');
	const accountWorkers = $derived((networkStatus?.hub?.account_network?.devices ?? []).filter((d) => d.status === 'active' && d.kind !== 'consumer').map((d) => d.name || 'a worker'));
	const recoveryAvailable = $derived(Boolean(networkStatus?.hub?.account_network?.recovery_available));
	const enrollmentPending = $derived(!enrolled && (networkStatus?.enrollment?.status === 'pending' || networkStatus?.enrollment?.status === 'syncing'));

	function stopNetworkPoll() { if (networkPoll) clearInterval(networkPoll); networkPoll = null; }
	function startNetworkPoll() { stopNetworkPoll(); networkPoll = setInterval(refreshNetwork, 3000); }

	async function refreshNetwork() {
		try { networkStatus = await getNetworkStatus(); }
		catch { /* the next poll retries */ }
	}

	function choose(next: StartChoice) {
		choice = next; networkError = '';
		void refreshNetwork();
		startNetworkPoll();
	}

	function backToChoices() { choice = null; networkError = ''; stopNetworkPoll(); }

	async function cleanInstall() {
		networkBusy = true; networkError = ''; stopNetworkPoll();
		try {
			const status = await getNetworkStatus();
			if (!status.enrolled) await createNetwork();
			networkStatus = await getNetworkStatus();
			finishStartStep();
		} catch (e: unknown) {
			networkError = getErrorMessage(e, 'Could not prepare this worker. Please try again.');
		} finally { networkBusy = false; }
	}

	async function unlockWithRecovery() {
		networkBusy = true; networkError = '';
		try {
			await unlockWithRecoveryKey(recoveryKeyInput, workerName.trim() || undefined);
			recoveryKeyInput = ''; showRecovery = false;
			await refreshNetwork();
		} catch (e: unknown) {
			networkError = getErrorMessage(e, 'The recovery key was not accepted');
		} finally { networkBusy = false; }
	}

	function finishStartStep() {
		stopNetworkPoll();
		completedSteps.add('start');
		advanceStep();
	}

	// ── Workspace ────────────────────────────────────────────────────────────
	let workspacePath = $state('');
	let workspaceError = $state('');
	let savingWorkspace = $state(false);

	function handleDirectorySelect(path: string) { workspacePath = path; workspaceError = ''; }

	async function saveWorkspaceAndContinue() {
		if (!workspacePath.trim()) { workspaceError = 'Please select or enter a workspace path'; return; }
		savingWorkspace = true; workspaceError = '';
		try {
			await setWorkspaceConfig(workspacePath);
			if (auth.current.status?.setup_required) await setupInstallation();
			await auth.checkAuth();
			completedSteps.add('workspace');
			advanceStep();
		} catch (e: unknown) {
			workspaceError = getErrorMessage(e, 'Failed to save workspace configuration');
		} finally { savingWorkspace = false; }
	}

	// ── Providers & GitHub ──────────────────────────────────────────────────
	let addProviderOpen = $state(false);

	function closeAddProvider() {
		addProviderOpen = false;
		auth.checkAuth().catch(() => {});
		providers.refreshProviders().catch(() => {});
	}

	async function loadProvidersStep() {
		providers.ensureLoaded();
		try { await auth.checkAuth(); } catch { /* non-critical */ }
	}

	// ── Navigation ───────────────────────────────────────────────────────────
	function advanceStep() {
		const idx = steps.findIndex((s) => s.id === currentStep);
		if (idx < steps.length - 1) {
			currentStep = steps[idx + 1].id;
			if (currentStep === 'providers') void loadProvidersStep();
		}
	}

	function goBack() {
		const idx = steps.findIndex((s) => s.id === currentStep);
		if (idx > 0) currentStep = steps[idx - 1].id;
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

	// ── Lifecycle ────────────────────────────────────────────────────────────
	onMount(async () => {
		try {
			workspaceConfig = await getWorkspaceConfig();
			workspacePath = workspaceConfig.workspace_path || workspaceConfig.default_path;
		} catch (e) {
			console.error('Failed to load workspace config:', e);
		}
		// A worker that already belongs to a network (an upgraded install, or a
		// wizard reopened after choosing) skips the Start step.
		try {
			networkStatus = await getNetworkStatus();
			startStepNeeded = !networkStatus.enrolled || networkStatus.network?.bootstrap_state === 'syncing';
			if (startStepNeeded) {
				currentStep = 'start';
				if (networkStatus.enrollment) choose('invite');
				else if (networkStatus.hub?.linked || networkStatus.network?.bootstrap_state === 'syncing') choose('account');
			}
		} catch {
			startStepNeeded = false;
		}
		loadingConfig = false;
	});

	onDestroy(() => {
		stopNetworkPoll();
	});
</script>

<svelte:head>
	<title>Setup - AI Shuffle</title>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
</svelte:head>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card p-6 sm:p-8 w-full transition-all duration-300 {currentStep === 'workspace' || currentStep === 'start' ? 'max-w-xl' : 'max-w-lg'}">
		{#if loadingConfig}
			<div class="text-center py-8">
				<Loader2 size={32} strokeWidth={1.5} class="mx-auto mb-4 animate-spin text-muted-foreground" />
				<p class="text-muted-foreground">Loading...</p>
			</div>
		{:else}
			{#if setupError}<p role="alert" class="text-destructive mb-4">{setupError}</p>{/if}
			<div class="text-center mb-6 sm:mb-8">
				<h1 class="text-xl sm:text-2xl font-bold text-foreground mb-2">Welcome to AI Shuffle</h1>
				<p class="text-muted-foreground text-sm sm:text-base">
					{#if currentStep === 'start'}
						Your worker is running. Start clean, or bring in the settings you already use.
					{:else if currentStep === 'workspace'}
						Choose where project checkouts will be created on this machine
					{:else}
						Optional — add model providers and connect GitHub. Everything here is also in Settings.
					{/if}
				</p>
			</div>

			<SetupProgress {steps} current={currentStep} completed={completedSteps} />

			<!-- ═══ Step 1: Start ═══ -->
			{#if currentStep === 'start'}
				<div class="space-y-4">
					{#if ready}
						<div class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2">
							<Check size={16} strokeWidth={2} />
							<span>{choice === 'clean' || !choice ? 'This worker is ready.' : `This worker now syncs settings with ${networkStatus?.network?.name || 'your other workers'}.`}</span>
						</div>
						<p class="text-sm text-muted-foreground">The remaining steps set up this machine only: its workspace folder, model providers and GitHub login.</p>
						<button type="button" onclick={finishStartStep} class="btn btn-primary w-full py-3">Continue</button>
					{:else if syncing}
						<div role="status" class="text-sm text-muted-foreground flex items-start gap-2"><Loader2 size={16} class="animate-spin shrink-0" /> Copying settings from your other worker. This continues in the background when it is available.</div>
						<button type="button" onclick={refreshNetwork} class="btn btn-secondary w-full py-3">Check sync status</button>
						<button type="button" onclick={finishStartStep} class="btn btn-primary w-full py-3">Continue setup</button>
					{:else if choice === null}
						<div class="grid gap-3">
							<button type="button" onclick={cleanInstall} disabled={networkBusy} class="text-left bg-muted/30 hover:bg-primary/10 border border-border hover:border-primary/40 rounded-lg p-4 transition-colors">
								<div class="flex items-start gap-3">
									<Sparkles size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
									<div>
										<h3 class="text-foreground font-medium mb-1">Clean install</h3>
										<p class="text-muted-foreground text-sm">Fresh settings on this computer. You can sign in or connect other devices any time later.</p>
									</div>
								</div>
							</button>
							<button type="button" onclick={() => choose('account')} disabled={networkBusy} class="text-left bg-muted/30 hover:bg-primary/10 border border-border hover:border-primary/40 rounded-lg p-4 transition-colors">
								<div class="flex items-start gap-3">
									<UserRound size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
									<div>
										<h3 class="text-foreground font-medium mb-1">Sign in to your Shuffle account</h3>
										<p class="text-muted-foreground text-sm">Sync settings, profiles and provider keys from your other workers, and control this one from your phone. New accounts start clean.</p>
									</div>
								</div>
							</button>
							<button type="button" onclick={() => choose('invite')} disabled={networkBusy} class="text-left bg-muted/30 hover:bg-primary/10 border border-border hover:border-primary/40 rounded-lg p-4 transition-colors">
								<div class="flex items-start gap-3">
									<Link2 size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
									<div>
										<h3 class="text-foreground font-medium mb-1">Join with an invite</h3>
										<p class="text-muted-foreground text-sm">Another worker on your network shows a QR code and link under Add device. Paste it here; no account needed.</p>
									</div>
								</div>
							</button>
						</div>
					{:else if choice === 'account'}
						<div class="space-y-3">
							{#if !linked}
								<div>
									<label for="workerName" class="block text-xs text-muted-foreground mb-1">Name for this worker</label>
									<input type="text" id="workerName" bind:value={workerName} maxlength="80" class="input text-sm" placeholder="My worker" />
								</div>
							{/if}
							<div class="bg-muted/30 border border-border rounded-lg p-4">
								<AccountPanel {workerName} variant="setup" />
							</div>
							{#if waitingOnAccount}
								<div class="space-y-3 bg-muted/30 border border-border rounded-lg p-4" role="status">
									<div class="flex items-center gap-2 text-sm text-foreground font-medium">
										<Loader2 size={14} strokeWidth={2} class="animate-spin" />
										Joining your account's network
									</div>
									<p class="text-sm text-muted-foreground">
										{#if accountWorkers.length}Your account already syncs settings between {accountWorkers.join(', ')}. This worker joins as soon as one of them is online; you can keep going with setup meanwhile.{:else}This worker joins as soon as another worker on your account is online; you can keep going with setup meanwhile.{/if}
									</p>
									{#if recoveryAvailable}
										{#if showRecovery}
											<div class="flex gap-2">
												<input type="text" bind:value={recoveryKeyInput} class="input flex-1 text-sm font-mono" placeholder="A1-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-X" autocomplete="off" spellcheck="false" aria-label="Recovery key" />
												<button type="button" onclick={unlockWithRecovery} class="btn btn-primary px-4" disabled={networkBusy || recoveryKeyInput.trim().length < 20}>
													{#if networkBusy}<Loader2 size={14} strokeWidth={2} class="animate-spin" />{:else}Restore{/if}
												</button>
											</div>
										{:else}
											<button type="button" onclick={() => (showRecovery = true)} class="btn btn-secondary w-full py-2.5 text-sm flex items-center justify-center gap-2">
												<KeyRound size={16} strokeWidth={1.5} /> No other worker available? Use your recovery key
											</button>
										{/if}
									{/if}
								</div>
							{:else if linked}
								<div role="status" class="text-sm text-muted-foreground flex items-start gap-2"><Loader2 size={16} class="animate-spin shrink-0" /> Connecting this worker to your account…</div>
							{/if}
						</div>
						<div class="flex gap-3">
							<button type="button" onclick={backToChoices} class="btn btn-secondary flex-1 py-3" disabled={networkBusy}><ChevronLeft size={16} class="inline-block" /> Back</button>
							{#if waitingOnAccount}
								<button type="button" onclick={finishStartStep} class="btn btn-primary flex-1 py-3">Continue setup</button>
							{/if}
						</div>
					{:else if choice === 'invite'}
						<div class="bg-muted/30 border border-border rounded-lg p-4">
							<JoinNetworkPanel status={networkStatus} deviceName={workerName} onChanged={refreshNetwork} />
						</div>
						{#if !enrollmentPending}
							<div class="flex gap-3">
								<button type="button" onclick={backToChoices} class="btn btn-secondary flex-1 py-3" disabled={networkBusy}><ChevronLeft size={16} class="inline-block" /> Back</button>
							</div>
						{/if}
					{/if}
					{#if networkError}
						<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm flex items-start gap-2">
							<AlertTriangle size={14} strokeWidth={2} class="mt-0.5 shrink-0" />
							<span>{networkError}</span>
						</div>
					{/if}
				</div>

			<!-- ═══ Step 2: Workspace ═══ -->
			{:else if currentStep === 'workspace'}
				<div class="space-y-4">
					<div class="bg-primary/10 border border-primary/30 rounded-lg p-4">
						<div class="flex items-start gap-3">
							<FolderOpen size={20} strokeWidth={1.5} class="text-primary mt-0.5 shrink-0" />
							<div>
								<h3 class="text-foreground font-medium mb-1">Select Workspace Folder</h3>
								<p class="text-muted-foreground text-sm">This is where project checkouts will be created on this machine.</p>
							</div>
						</div>
					</div>
					<DirectoryBrowser onSelect={handleDirectorySelect} selectedPath={workspacePath} />
					{#if workspacePath}
						<div class="text-sm text-foreground font-mono bg-muted/50 px-3 py-2 rounded-lg border border-border flex items-center gap-2">
							<FolderOpen size={14} strokeWidth={1.5} class="text-primary shrink-0" />
							<span class="truncate">{workspacePath}</span>
						</div>
					{/if}
					<div>
						<label for="workspaceManual" class="block text-xs text-muted-foreground mb-1">Or type a path manually</label>
						<input type="text" id="workspaceManual" bind:value={workspacePath} class="input font-mono text-sm" placeholder="Enter workspace path" />
					</div>
					{#if workspaceError}
						<div class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{workspaceError}</div>
					{/if}
					<div class="flex gap-3">
						{#if startStepNeeded}<button type="button" onclick={goBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
						<button type="button" onclick={saveWorkspaceAndContinue} class="btn btn-primary flex-1 py-3" disabled={!workspacePath.trim() || savingWorkspace}>
							{#if savingWorkspace}<Loader2 size={16} strokeWidth={2} class="inline-block animate-spin mr-2" />{/if}
							Continue
						</button>
					</div>
				</div>

			<!-- ═══ Step 3: Providers & GitHub ═══ -->
			{:else if currentStep === 'providers'}
				<div class="space-y-6">
					<div class="space-y-3">
						<div class="flex items-center gap-2">
							<Plus size={16} strokeWidth={1.5} class="text-foreground" />
							<span class="text-sm font-semibold text-foreground">Model providers</span>
							<span class="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground border border-border font-medium">Optional</span>
						</div>
						{#if providers.current.providers.length === 0}
							<p class="text-sm text-muted-foreground">Connect a Claude, OpenAI, Grok or OpenRouter account or API key to start chatting. {#if enrolled && networkStatus?.network && networkStatus.network.member_count > 1}Providers synced from your other workers appear here once sync finishes.{/if}</p>
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
						<button type="button" onclick={() => (addProviderOpen = true)} class="btn btn-secondary w-full py-2.5 text-sm flex items-center justify-center gap-2">
							<Plus size={16} strokeWidth={1.5} /> Add provider
						</button>
					</div>

					<div class="border-t border-border"></div>

					<GitHubConnection />

					<div class="border-t border-border pt-4">
						<div class="flex gap-3">
							<button type="button" onclick={goBack} class="btn btn-secondary flex-1 py-3">Back</button>
							<button type="button" onclick={finishSetup} class="btn btn-primary flex-1 py-3">{$anyCredential ? 'Finish Setup' : 'Finish without a provider'}</button>
						</div>
						{#if !$anyCredential}
							<p class="text-xs text-muted-foreground text-center mt-2">You can add a model provider later in Settings → Providers.</p>
						{/if}
					</div>
				</div>
			{/if}

			{#if addProviderOpen}
				<AddProviderModal onClose={closeAddProvider} />
			{/if}
		{/if}
	</div>
</div>
