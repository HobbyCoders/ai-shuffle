<script lang="ts">
	/**
	 * First-run setup (wizard v2). Docker, Unraid and the desktop app all land
	 * here until setup is finished. Steps, in order:
	 *
	 * 1. Welcome: what AI Shuffle is, and the disclosures (agents act with this
	 *    worker's access, your own AI account, update checks).
	 * 2. Owner password. Before a password exists a browser cannot reach this
	 *    page, so the owner gate (OwnerAccessGate) shows steps 1 and 2 itself;
	 *    here the step only appears as done. The desktop app has no such step.
	 * 3. License: a key, or the beta, which needs none.
	 * 4. AI provider: connect at least one of the agent providers (Claude,
	 *    OpenAI, xAI Grok, OpenRouter, a custom gateway), all equal choices.
	 *    Blocking: there is no way to finish setup without one ready.
	 * 5. Workspace, with the host folder behind it on Docker and Unraid.
	 * 6. First run: one short prompt on a ready provider, the reply streamed in.
	 * 7. Optional: HobbyCoders account (or a worker invite), GitHub, and
	 *    earlier conversations found on this worker, when there are any.
	 */
	import { goto } from '$app/navigation';
	import { onMount, onDestroy } from 'svelte';
	import { SvelteSet } from 'svelte/reactivity';
	import { auth, isAdmin } from '$lib/stores/auth.svelte';
	import { ownerAccess } from '$lib/stores/owner-access.svelte';
	import {
		getWorkspaceConfig, setupInstallation, setWorkspaceConfig,
		completeOnboarding
	} from '$lib/api/auth';
	import { getErrorMessage, type WorkspaceConfig } from '$lib/api/client';
	import { createNetwork, getNetworkStatus, type NetworkStatus } from '$lib/api/network';
	import SetupProgress from '$lib/components/SetupProgress.svelte';
	import GitHubConnection from '$lib/components/GitHubConnection.svelte';
	import AccountPanel from '$lib/components/deck/cards/settings/AccountPanel.svelte';
	import JoinNetworkPanel from '$lib/components/deck/cards/settings/JoinNetworkPanel.svelte';
	import WelcomeStep from '$lib/components/setup/WelcomeStep.svelte';
	import LicenseStep from '$lib/components/setup/LicenseStep.svelte';
	import ProviderStep from '$lib/components/setup/ProviderStep.svelte';
	import WorkspaceStep from '$lib/components/setup/WorkspaceStep.svelte';
	import FirstRunStep from '$lib/components/setup/FirstRunStep.svelte';
	import CliHistoryImport from '$lib/components/setup/CliHistoryImport.svelte';
	import {
		hasOwnerStep, markWelcomeSeen, setupSteps, welcomeSeen, type SetupStepId
	} from '$lib/components/setup/setup-steps';
	import { Check, Loader2, ChevronLeft } from 'lucide-svelte';

	// ── Steps ────────────────────────────────────────────────────────────────
	let loadingConfig = $state(true);
	let setupError = $state('');
	let ownerStep = $state(false);
	let desktop = $state(false);
	const steps = $derived(setupSteps({ ownerStep }));
	let currentStep = $state<SetupStepId>('welcome');
	// A reactive set: adding a step must redraw its checkmark in the progress bar.
	const completedSteps = new SvelteSet<SetupStepId>();

	const SUBTITLES: Record<SetupStepId, string> = {
		welcome: 'A few steps to set up this worker. It takes about five minutes.',
		owner: 'The password every browser on your network signs in with.',
		license: 'Your AI Shuffle license.',
		provider: 'Connect at least one AI provider. You can add more later in Settings > Providers.',
		workspace: 'Where your projects live.',
		'first-run': 'Watch an agent answer.',
		extras: 'Optional: your HobbyCoders account, GitHub and earlier conversations.',
	};

	function goTo(step: SetupStepId) { currentStep = step; setupError = ''; }

	function complete(step: SetupStepId) {
		completedSteps.add(step);
		const idx = steps.findIndex((s) => s.id === step);
		// The owner step is always behind us here: its screen is the sign-in gate.
		let next = idx + 1;
		if (next < steps.length && steps[next].id === 'owner') next++;
		if (idx >= 0 && next < steps.length) goTo(steps[next].id);
	}

	function back(step: SetupStepId) {
		const idx = steps.findIndex((s) => s.id === step);
		// The owner step is behind us for good: its screen is the sign-in gate.
		let prev = idx - 1;
		if (prev >= 0 && steps[prev].id === 'owner') prev--;
		if (prev >= 0) goTo(steps[prev].id);
	}

	// ── Welcome ──────────────────────────────────────────────────────────────
	function continueFromWelcome() { markWelcomeSeen(); complete('welcome'); }

	// ── AI provider ─────────────────────────────────────────────────────────
	// The provider connected last in this visit; the first run tries it first.
	let connectedProvider = $state<string | null>(null);

	// ── Workspace ────────────────────────────────────────────────────────────
	let workspaceConfig = $state<WorkspaceConfig | null>(null);
	let workspacePath = $state('');
	let workspaceError = $state('');
	let savingWorkspace = $state(false);
	const hostWorkspacePath = $derived((workspaceConfig as (WorkspaceConfig & { host_path?: string | null }) | null)?.host_path ?? null);

	async function saveWorkspaceAndContinue() {
		if (!workspacePath.trim()) { workspaceError = 'Please select or enter a workspace path'; return; }
		savingWorkspace = true; workspaceError = '';
		try {
			await setWorkspaceConfig(workspacePath);
			if (auth.current.status?.setup_required) await setupInstallation();
			await auth.checkAuth();
			complete('workspace');
		} catch (e: unknown) {
			workspaceError = getErrorMessage(e, 'Failed to save workspace configuration');
		} finally { savingWorkspace = false; }
	}

	// ── Account and GitHub ──────────────────────────────────────────────────
	type NetworkChoice = 'account' | 'invite';
	let networkChoice = $state<NetworkChoice>('account');
	let networkStatus = $state<NetworkStatus | null>(null);
	let networkStepNeeded = $state(false);
	let workerName = $state('My worker');
	let networkPoll: ReturnType<typeof setInterval> | null = null;
	let finishing = $state(false);

	const enrolled = $derived(Boolean(networkStatus?.enrolled));
	const syncing = $derived(enrolled && networkStatus?.network?.bootstrap_state === 'syncing');
	const linked = $derived(Boolean(networkStatus?.hub?.linked));
	const waitingOnAccount = $derived(!enrolled && linked && networkStatus?.hub?.status === 'pending');
	const accountWorkers = $derived((networkStatus?.hub?.account_network?.devices ?? []).filter((d) => d.status === 'active' && d.kind !== 'consumer').map((d) => d.name || 'a worker'));
	const joinedOther = $derived(enrolled && (networkStatus?.network?.member_count ?? 1) > 1);

	function stopNetworkPoll() { if (networkPoll) clearInterval(networkPoll); networkPoll = null; }
	function startNetworkPoll() { stopNetworkPoll(); networkPoll = setInterval(refreshNetwork, 3000); }
	async function refreshNetwork() {
		try { networkStatus = await getNetworkStatus(); }
		catch { /* the next poll retries */ }
	}
	function chooseNetwork(next: NetworkChoice) { networkChoice = next; void refreshNetwork(); }

	$effect(() => {
		if (currentStep === 'extras' && networkStepNeeded) startNetworkPoll();
		else stopNetworkPoll();
	});

	async function finishSetup() {
		if (finishing) return;
		finishing = true; setupError = '';
		try {
			// A worker that joined nothing becomes its own network (a clean
			// install), exactly as "Continue without an account" did before.
			if (networkStepNeeded && !linked && !(networkStatus?.enrollment)) {
				const status = await getNetworkStatus();
				if (!status.enrolled) await createNetwork();
			}
			stopNetworkPoll();
			await completeOnboarding();
			await auth.checkAuth();
			goto('/');
		} catch (e: unknown) {
			setupError = getErrorMessage(e, 'Setup could not be saved. Please try again.');
		} finally {
			finishing = false;
		}
	}

	// ── Lifecycle ────────────────────────────────────────────────────────────
	onMount(async () => {
		const [workspace, network, owner] = await Promise.allSettled([
			getWorkspaceConfig(),
			getNetworkStatus(),
			ownerAccess.status ? Promise.resolve(ownerAccess.status) : ownerAccess.refresh(),
		]);
		if (workspace.status === 'fulfilled') {
			workspaceConfig = workspace.value;
			workspacePath = workspace.value.workspace_path || workspace.value.default_path;
		} else {
			console.error('Failed to load workspace config:', workspace.reason);
		}
		if (network.status === 'fulfilled') {
			networkStatus = network.value;
			networkStepNeeded = !network.value.enrolled || network.value.network?.bootstrap_state === 'syncing';
			if (network.value.enrollment) networkChoice = 'invite';
		}
		const ownerStatus = owner.status === 'fulfilled' ? owner.value : null;
		desktop = Boolean(ownerStatus?.desktop_handoff) || auth.current.status?.claude_cli?.login?.surface === 'desktop';
		ownerStep = !desktop && hasOwnerStep(ownerStatus);
		if (ownerStep) completedSteps.add('owner');
		if (welcomeSeen()) {
			completedSteps.add('welcome');
			currentStep = 'license';
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

<div class="setup-viewport flex flex-col items-center overflow-y-auto p-4">
	<div class="card my-auto shrink-0 p-6 sm:p-8 w-full max-w-xl">
		{#if loadingConfig}
			<div class="text-center py-8">
				<Loader2 size={32} strokeWidth={1.5} class="mx-auto mb-4 animate-spin text-muted-foreground" />
				<p class="text-muted-foreground">Loading...</p>
			</div>
		{:else}
			<div class="text-center mb-6 sm:mb-8">
				<h1 class="text-xl sm:text-2xl font-bold text-foreground mb-2">Welcome to AI Shuffle</h1>
				<p class="text-muted-foreground text-sm sm:text-base">{SUBTITLES[currentStep]}</p>
			</div>

			<SetupProgress {steps} current={currentStep} completed={completedSteps} />

			{#if setupError}<p role="alert" class="text-destructive text-sm mb-4">{setupError}</p>{/if}

			{#if currentStep === 'welcome'}
				<WelcomeStep {desktop} onContinue={continueFromWelcome} />

			{:else if currentStep === 'license'}
				<LicenseStep onContinue={() => complete('license')} onBack={() => back('license')} />

			{:else if currentStep === 'provider'}
				<ProviderStep isAdmin={$isAdmin} onConnected={(id) => (connectedProvider = id)}
					onContinue={() => complete('provider')} onBack={() => back('provider')} />

			{:else if currentStep === 'workspace'}
				<WorkspaceStep bind:path={workspacePath} container={workspaceConfig ? !workspaceConfig.is_local_mode : false}
					hostPath={hostWorkspacePath} saving={savingWorkspace} error={workspaceError}
					onSave={saveWorkspaceAndContinue} onBack={() => back('workspace')} />

			{:else if currentStep === 'first-run'}
				<FirstRunStep preferredProvider={connectedProvider} onContinue={() => complete('first-run')} onBack={() => back('first-run')}
					onFixProvider={() => { completedSteps.delete('provider'); goTo('provider'); }} />

			{:else if currentStep === 'extras'}
				<div class="space-y-6" data-testid="setup-extras">
					{#if networkStepNeeded}
						<section class="space-y-3" aria-label="HobbyCoders account">
							{#if syncing}
								<div role="status" class="text-sm text-muted-foreground flex items-start gap-2"><Loader2 size={16} class="animate-spin shrink-0" /> Copying settings from your other worker. This continues in the background.</div>
							{:else if joinedOther}
								<div class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2">
									<Check size={16} strokeWidth={2} />
									<span>This worker now syncs settings with {networkStatus?.network?.name || 'your other workers'}.</span>
								</div>
							{:else if networkChoice === 'account'}
								<p class="text-sm text-muted-foreground">Sign in to link this worker to the Android app, your other workers and remote access. Local use never needs an account.</p>
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
									<div class="space-y-1 bg-muted/30 border border-border rounded-lg p-4" role="status">
										<div class="flex items-center gap-2 text-sm text-foreground font-medium">
											<Loader2 size={14} strokeWidth={2} class="animate-spin" />
											Joining your account's network
										</div>
										<p class="text-sm text-muted-foreground">
											{#if accountWorkers.length}Your account already syncs settings between {accountWorkers.join(', ')}. This worker joins as soon as one of them is online.{:else}This worker joins as soon as another worker on your account is online.{/if}
										</p>
									</div>
								{:else if linked}
									<div role="status" class="text-sm text-muted-foreground flex items-start gap-2"><Loader2 size={16} class="animate-spin shrink-0" /> Connecting this worker to your account…</div>
								{/if}
								{#if !linked}
									<button type="button" onclick={() => chooseNetwork('invite')} class="btn btn-secondary w-full py-2.5 text-sm">Use a worker invite instead</button>
								{/if}
							{:else}
								<div class="bg-muted/30 border border-border rounded-lg p-4">
									<JoinNetworkPanel status={networkStatus} deviceName={workerName} onChanged={refreshNetwork} />
								</div>
								{#if !(networkStatus?.enrollment)}
									<button type="button" onclick={() => chooseNetwork('account')} class="btn btn-secondary w-full py-2.5 text-sm"><ChevronLeft size={16} class="inline-block" /> Back to account sign-in</button>
								{/if}
							{/if}
						</section>
						<div class="border-t border-border"></div>
					{/if}

					<GitHubConnection />

					<CliHistoryImport />

					<div class="border-t border-border pt-4 flex gap-3">
						<button type="button" onclick={() => back('extras')} class="btn btn-secondary flex-1 py-3" disabled={finishing}>Back</button>
						<button type="button" onclick={finishSetup} class="btn btn-primary flex-1 py-3" disabled={finishing}>
							{#if finishing}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Finish
						</button>
					</div>
				</div>
			{/if}
		{/if}
	</div>
</div>

<style>
	/* The app disables document scrolling, so setup owns its scroll area.
	   Auto margins center a short card and collapse when it needs to scroll. */
	.setup-viewport {
		height: 100vh;
		height: var(--app-viewport-height, 100dvh);
	}
</style>
