<script lang="ts">
	/**
	 * The Claude subscription: the stock CLI's own sign-in on this worker.
	 * Overview has the CLI facts and the sign-in (in-app terminal on the
	 * desktop, the host command on the phone); Usage the unattended budget, the
	 * plan windows and a link to claude.ai's usage page; Policy the loop guard and
	 * held turns.
	 *
	 * Where the worker saves several sign-ins, this is also one saved entry:
	 * rename it, make it the active one, or remove it. A saved entry that is
	 * not active shows its last `claude auth status` (the CLI facts follow the
	 * active folder only).
	 */
	import { onDestroy, onMount } from 'svelte';
	import { RefreshCw, SquareTerminal } from 'lucide-svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { getErrorMessage } from '$lib/api/client';
	import TextField from '../rows/TextField.svelte';
	import CredentialControl from './CredentialControl.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import CodeBlock from '../rows/CodeBlock.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ClaudeCliTerminal from './setup/ClaudeCliTerminal.svelte';
	import ClaudeCliInstall from './setup/ClaudeCliInstall.svelte';
	import BuiltInModels from './tabs/BuiltInModels.svelte';
	import ClaudeUsage from './tabs/ClaudeUsage.svelte';
	import ClaudePolicy from './tabs/ClaudePolicy.svelte';
	import { ClaudeCliLogin } from './claude-cli-login.svelte';
	import { LoopGuard } from './loop-guard.svelte';
	import { claudePlan, type CredentialView, type ProviderView } from './providers-model';
	import { changeClaudeMode, startSetup } from './provider-actions';
	import { layout } from '$lib/stores/layout.svelte';
	import { terminals } from '$lib/stores/terminals.svelte';
	import { terminalAccess } from '$lib/terminal/gating';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const access = getSettingsAccess();
	const guard = new LoopGuard();
	const cli = new ClaudeCliLogin({
		onStatus: (next) => {
			// Keep the mode summary (list rows, header) in step with the CLI.
			if (!!providers.current.claude?.cli.signed_in !== !!next.signed_in) void providers.loadClaude();
			// Signed in again to the active saved entry: re-read its identity
			// (activating the active entry re-runs its `claude auth status`).
			const saved = credential.subscription;
			if (saved?.active && saved.needs_sign_in && next.signed_in) {
				void providers.activateClaudeSubscriptionAccount(saved.id).catch(() => {});
			}
		}
	});

	/** A saved sign-in entry (several per worker); null for the single CLI sign-in. */
	const account = $derived(credential.subscription ?? null);
	/** A saved entry that is not the one Claude Code uses: the CLI facts are not about it. */
	const inactive = $derived(!!account && !account.active);
	const several = $derived(!!provider.claudeSubscriptions);
	let saving = $state(false);
	let accountError = $state('');

	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
	const status = $derived(cli.status);
	const signedIn = $derived(status ? !!status.signed_in : !!providers.current.claude?.cli.signed_in);
	const canUseTerminal = $derived(cli.terminalAvailable(!access.readOnly, platform.isMobile));
	const summaryCli = $derived(providers.current.claude?.cli);
	// A shell with no chat (the Terminal page), for sign-in or any other setup
	// step. The worker's terminal probe is the gate: it refuses non-owners and
	// agent previews.
	const shellReady = $derived(!access.readOnly && terminalAccess(terminals.capability).kind === 'ready');

	const position = $derived(
		inactive
			? 'Saved, not active'
			: mode === 'off'
			? 'Claude is off'
			: mode !== 'subscription'
				? 'Standby for Claude'
				: signedIn
					? 'Runs every Claude session'
					: 'Selected, not ready'
	);

	const facts = $derived(inactive ? [
		{ label: 'Account', value: account?.email ?? null },
		{ label: 'Plan', value: claudePlan(account?.plan) },
		{ label: 'Organization', value: account?.org_name ?? null },
		{ label: 'Checked', value: checkedLabel(account?.checked_at) }
	] : [
		{ label: 'Account', value: signedIn ? (status?.email ?? summaryCli?.email ?? null) : 'None' },
		{ label: 'Plan', value: claudePlan(status?.plan ?? summaryCli?.plan) },
		{
			label: 'Unattended',
			value: guard.status?.applies
				? `${guard.status.usage.install_last_hour} of ${guard.status.install_turns_per_hour} this hour`
				: null
		},
		{ label: 'CLI', value: status?.cli_version ?? null, mono: true }
	]);

	const tabs = $derived(inactive ? [{ id: 'Overview' }, { id: 'Models' }] : [
		{ id: 'Overview' },
		{ id: 'Models' },
		{ id: 'Usage' },
		{ id: 'Policy', count: guard.status?.held.length || undefined }
	]);

	async function signOut() {
		await cli.signOut();
		await providers.loadClaude(true);
	}

	function checkedLabel(at: number | null | undefined): string | null {
		if (!at) return null;
		return new Date(at * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
	}

	async function rename(label: string) {
		if (!account || label.trim() === account.label || saving) return;
		saving = true;
		accountError = '';
		try {
			await providers.renameClaudeSubscriptionAccount(account.id, label);
			flashSaved();
		} catch (e) {
			accountError = getErrorMessage(e, 'Could not rename this sign-in');
		} finally {
			saving = false;
		}
	}

	async function remove() {
		if (!account) return;
		const back = provider.item;
		accountError = '';
		try {
			await providers.deleteClaudeSubscriptionAccount(account.id);
			settingsNav.selectItem(back, 'providers');
		} catch (e) {
			accountError = getErrorMessage(e, 'Could not remove this sign-in');
		}
	}

	const removeAction = $derived<DangerAction | null>(
		account
			? {
					label: account.kind === 'default' ? 'Forget this sign-in' : 'Remove this sign-in',
					hint:
						account.kind === 'default'
							? 'Claude Code’s own sign-in stays signed in'
							: account.active
								? 'Signs it out; Claude Code goes back to its own sign-in'
								: 'Signs it out and removes it from this worker',
					button: 'Remove',
					question: `Remove ${credential.name}?`,
					onConfirm: remove
				}
			: null
	);

	const danger = $derived<DangerAction[]>([
		...(signedIn && !inactive
			? [
					{
						id: 'providers.cliSignOut',
						label: 'Sign out of the Claude CLI',
						button: 'Sign out',
						question: 'Sign out of the CLI?',
						onConfirm: signOut
					}
				]
			: []),
		...(removeAction ? [removeAction] : [])
	]);

	function startSignIn() {
		settingsNav.setTab('providers', settingsNav.selectedItem('providers') ?? credential.item, 'Overview');
		void cli.startLogin();
	}

	/** A saved entry that is not active: make it the active one, then sign in to its folder. */
	async function signInAgain() {
		if (!account) return;
		accountError = '';
		try {
			if (!account.active) await providers.activateClaudeSubscriptionAccount(account.id);
			await cli.load(true);
			if (cli.terminalAvailable(!access.readOnly, platform.isMobile) && !cli.status?.signed_in) startSignIn();
		} catch (e) {
			accountError = getErrorMessage(e, 'Could not switch to this sign-in');
		}
	}

	onMount(() => {
		void cli.init(() => canUseTerminal);
		void terminals.ensureCapability();
		void guard.load();
		void providers.loadUsage();
	});
	onDestroy(() => cli.dispose());
</script>

<DetailPane
	name={credential.name}
	badge="Subscription"
	status={credential.status}
	statusText={credential.statusText}
	subtitle={position}
	{facts}
	{tabs}
	{danger}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		{#if account}<CredentialControl {credential} />{/if}
		{#if inactive}
			{#if account?.needs_sign_in}
				<button type="button" class="st-btn primary" disabled={access.readOnly} onclick={signInAgain}><SquareTerminal />Sign in again</button>
			{/if}
		{:else if mode !== 'subscription'}
			<button type="button" class="st-btn" onclick={() => changeClaudeMode('subscription')}>Use for Claude</button>
		{:else if !signedIn && canUseTerminal && status?.installed && !cli.terminalOpen}
			<button type="button" class="st-btn primary" onclick={startSignIn}><SquareTerminal />Sign in</button>
		{:else}
			<button type="button" class="st-btn" disabled={cli.checking} onclick={() => cli.load(true)}
				><RefreshCw />Check again</button
			>
		{/if}
	{/snippet}
	{#snippet banner()}
		{#if status?.relogin_notice}<InlineBanner tone="warn">{status.relogin_notice.message}</InlineBanner>{/if}
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Overview'}
			{#if several}
				<p class="pv-note">Save several Claude sign-ins on this worker; Claude Code uses one at a time. Switching keeps the Claude Code mode unchanged.</p>
			{:else}
				<p class="pv-note">Claude uses one subscription sign-in on this worker. To change it, sign out and sign in again. You can save multiple Anthropic API keys separately.</p>
			{/if}
			{#if account}
				<RowGroup title="Saved sign-in">
					<SettingRow label="Name">
						<TextField value={account.label} width={220} maxlength={120} disabled={saving || access.readOnly} label="Name" onchange={rename} />
					</SettingRow>
					<SettingRow label="Account selection" hint="Switching keeps the Claude Code mode unchanged">
						<ValueText value={credential.statusText} status={credential.status} />
					</SettingRow>
					{#if inactive && account.needs_sign_in}
						<SettingRow label="Sign in again" hint="Becomes the active subscription, then signs in">
							<button type="button" class="st-btn" disabled={access.readOnly} onclick={signInAgain}><SquareTerminal />Sign in again</button>
						</SettingRow>
					{/if}
					<SettingRow label="Another Subscription" hint="Becomes active after sign-in">
						<button type="button" class="st-btn" disabled={access.readOnly} onclick={() => startSetup(provider, 'cli')}>Add</button>
					</SettingRow>
				</RowGroup>
				{#if accountError}<p class="pv-msg err" role="alert">{accountError}</p>{/if}
			{/if}
		{/if}
		{#if inactive}
			{#if tab === 'Models'}<BuiltInModels />{/if}
		{:else if tab === 'Models'}
			<BuiltInModels />
		{:else if tab === 'Usage'}
			<ClaudeUsage {guard} />
		{:else if tab === 'Policy'}
			<ClaudePolicy {guard} {mode} />
		{:else if cli.terminalOpen}
			<RowGroup title="Sign in" bare>
				<ClaudeCliTerminal {cli} />
			</RowGroup>
		{:else if !status}
			<p class="pv-msg" role="status">{cli.loadError || 'Asking the Claude Code CLI'}</p>
		{:else if !status.installed}
			<ClaudeCliInstall isAdmin={!access.readOnly} onFinished={() => cli.load(true)} />
		{:else if signedIn}
			<RowGroup title="Claude CLI">
				{#if status.auth_method_label}
					<SettingRow label="Signed in with"><ValueText tone="fg" value={status.auth_method_label} /></SettingRow>
				{/if}
				{#if status.org_name}
					<SettingRow label="Organization"><ValueText tone="fg" value={status.org_name} /></SettingRow>
				{/if}
				{#if status.binary_path}
					<SettingRow label="Binary"><ValueText mono value={status.binary_path} /></SettingRow>
				{/if}
				{#if several && !account}
					<SettingRow label="Another Subscription" hint="Becomes active after sign-in">
						<button type="button" class="st-btn" onclick={() => startSetup(provider, 'cli')}>Add</button>
					</SettingRow>
				{:else if !several}
					<SettingRow label="Switch accounts" hint="Sign out, then sign in">
						<ValueText mono value={status.login.logout_command} />
					</SettingRow>
				{/if}
			</RowGroup>
			{#if status.error}<p class="pv-msg">{status.error}</p>{/if}
		{:else}
			<RowGroup title="Sign in">
				{#if !status.installed}
					<SettingRow label="Claude Code CLI" hint="Not installed on this worker">
						<ValueText status="warn" value="Not installed" />
					</SettingRow>
				{:else if canUseTerminal}
					<SettingRow label="Sign in on this worker" hint="Runs claude auth login">
						<button type="button" class="st-btn primary" onclick={startSignIn}><SquareTerminal />Sign in</button>
					</SettingRow>
				{/if}
				{#if status.installed}
					<SettingRow
						label={canUseTerminal ? 'Or on the host' : 'Run on the host'}
						hint={status.login.surface === 'docker' ? 'Open a terminal on the Docker host' : undefined}
					>
						<button type="button" class="st-btn" disabled={cli.checking} onclick={() => cli.load(true)}
							><RefreshCw />Check again</button
						>
					</SettingRow>
					<div class="cmd"><CodeBlock code={cli.hostCommand} prompt /></div>
				{/if}
			</RowGroup>
			{#if status.error}<p class="pv-msg">{status.error}</p>{/if}
		{/if}
		{#if shellReady && status && !inactive && !cli.terminalOpen && tab !== 'Models' && tab !== 'Usage' && tab !== 'Policy'}
			<RowGroup title="Terminal">
				<SettingRow label="Open a terminal" hint="A shell on this worker with no chat. Sign in by hand or finish another setup step.">
					<button type="button" class="st-btn" onclick={() => layout.openFeaturePage('terminal')}
						><SquareTerminal />Open terminal</button
					>
				</SettingRow>
			</RowGroup>
		{/if}
		{#if cli.loginError && !cli.terminalOpen && tab === 'Overview'}<p class="pv-msg err" role="alert">
				{cli.loginError}
			</p>{/if}
	{/snippet}
</DetailPane>

<style>
	.cmd {
		padding: 0 16px 14px;
	}
</style>
