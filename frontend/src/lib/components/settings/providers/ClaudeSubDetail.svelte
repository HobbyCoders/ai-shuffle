<script lang="ts">
	/**
	 * The Claude subscription: the stock CLI's own sign-in on this worker.
	 * Overview has the CLI facts and the sign-in (in-app terminal on the
	 * desktop, the host command on the phone); Usage the unattended budget, the
	 * plan windows and the weekly and 5-hour resets; Policy the loop guard and
	 * held turns.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { RefreshCw, SquareTerminal } from 'lucide-svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import CodeBlock from '../rows/CodeBlock.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ClaudeCliTerminal from './setup/ClaudeCliTerminal.svelte';
	import BuiltInModels from './tabs/BuiltInModels.svelte';
	import ClaudeUsage from './tabs/ClaudeUsage.svelte';
	import ClaudePolicy from './tabs/ClaudePolicy.svelte';
	import LimitResumeRows from './LimitResumeRows.svelte';
	import { ClaudeCliLogin } from './claude-cli-login.svelte';
	import { LoopGuard } from './loop-guard.svelte';
	import { ClaudeResets } from './claude-resets.svelte';
	import { claudePlan, CLAUDE_SUB_ITEM, type CredentialView, type ProviderView } from './providers-model';
	import { changeClaudeMode } from './provider-actions';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const access = getSettingsAccess();
	const guard = new LoopGuard();
	const resets = new ClaudeResets();
	const cli = new ClaudeCliLogin({
		onStatus: (next) => {
			// Keep the mode summary (list rows, header) in step with the CLI.
			if (!!providers.current.claude?.cli.signed_in !== !!next.signed_in) void providers.loadClaude();
		}
	});

	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
	const status = $derived(cli.status);
	const signedIn = $derived(status ? !!status.signed_in : !!providers.current.claude?.cli.signed_in);
	const canUseTerminal = $derived(cli.terminalAvailable(!access.readOnly, platform.isMobile));
	const summaryCli = $derived(providers.current.claude?.cli);

	const position = $derived(
		mode === 'off'
			? 'Claude is off'
			: mode !== 'subscription'
				? 'Standby for Claude'
				: signedIn
					? 'Runs every Claude session'
					: 'Selected, not ready'
	);

	const facts = $derived([
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

	const tabs = $derived([
		{ id: 'Overview' },
		{ id: 'Models' },
		{ id: 'Usage', count: resets.weekly?.eligible && resets.weeklyLeft ? resets.weeklyLeft : undefined },
		{ id: 'Policy', count: guard.status?.held.length || undefined }
	]);

	async function signOut() {
		await cli.signOut();
		await providers.loadClaude(true);
	}

	const danger = $derived<DangerAction[]>(
		signedIn
			? [
					{
						id: 'providers.cliSignOut',
						label: 'Sign out of the Claude CLI',
						button: 'Sign out',
						question: 'Sign out of the CLI?',
						onConfirm: signOut
					}
				]
			: []
	);

	function startSignIn() {
		settingsNav.setTab('providers', CLAUDE_SUB_ITEM, 'Overview');
		void cli.startLogin();
	}

	onMount(() => {
		void cli.init(() => canUseTerminal);
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
	item={CLAUDE_SUB_ITEM}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		{#if mode !== 'subscription'}
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
		{#if tab === 'Models'}
			<BuiltInModels />
		{:else if tab === 'Usage'}
			<ClaudeUsage {guard} resets={signedIn ? resets : null} />
		{:else if tab === 'Policy'}
			<ClaudePolicy {guard} {mode} />
			{#if mode !== 'off'}
				<LimitResumeRows providerKey="claude" ids={{ resume: 'providers.limitResume', compact: 'providers.limitCompact' }} />
			{/if}
		{:else if cli.terminalOpen}
			<RowGroup title="Sign in" bare>
				<ClaudeCliTerminal {cli} />
			</RowGroup>
		{:else if !status}
			<p class="pv-msg" role="status">{cli.loadError || 'Asking the Claude Code CLI'}</p>
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
				<SettingRow label="Switch accounts" hint="Sign out, then sign in">
					<ValueText mono value={status.login.logout_command} />
				</SettingRow>
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
						hint={status.login.surface === 'docker' ? 'Unraid: the Terminal button' : undefined}
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
