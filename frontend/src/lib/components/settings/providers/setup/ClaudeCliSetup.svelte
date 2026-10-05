<script lang="ts">
	/**
	 * ClaudeCliSetup: the Claude subscription, through the stock CLI's own
	 * sign-in. On the worker's desktop or Docker image the owner runs
	 * `claude auth login` in an in-app terminal; on the phone (and where the
	 * worker offers no terminal) the detail shows the command to run on the
	 * host, then Check again.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { RefreshCw, SquareTerminal } from 'lucide-svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { getErrorMessage } from '$lib/api/client';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import CodeBlock from '../../rows/CodeBlock.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';
	import { ClaudeCliLogin } from '../claude-cli-login.svelte';
	import { CLAUDE_SUB_ITEM } from '../providers-model';
	import ClaudeCliTerminal from './ClaudeCliTerminal.svelte';
	import ClaudeCliInstall from './ClaudeCliInstall.svelte';

	interface Props {
		/** Claude has no other credential: the subscription becomes the active one. */
		makeActive?: boolean;
		onFinish: (item: string) => void;
		onCancel: () => void;
	}

	let { makeActive = false, onFinish, onCancel }: Props = $props();

	const access = getSettingsAccess();
	const cli = new ClaudeCliLogin();
	const canUseTerminal = $derived(cli.terminalAvailable(!access.readOnly, platform.isMobile));
	const signedIn = $derived(!!cli.status?.signed_in);
	let finishing = $state(false);
	let error = $state('');

	async function done() {
		if (finishing) return;
		finishing = true;
		error = '';
		try {
			if (makeActive) await providers.setClaudeMode('subscription');
			await providers.loadClaude(true);
			onFinish(CLAUDE_SUB_ITEM);
		} catch (e) {
			error = getErrorMessage(e, 'Could not use the subscription for Claude');
		} finally {
			finishing = false;
		}
	}

	async function cancel() {
		if (cli.terminalOpen) await cli.closeTerminal();
		onCancel();
	}

	onMount(() => {
		void cli.init(() => canUseTerminal);
	});
	onDestroy(() => cli.dispose());
</script>

{#if !cli.status}
	<p class="pv-msg" role="status">{cli.loadError || 'Asking the Claude Code CLI'}</p>
{:else if !cli.status.installed}
	<ClaudeCliInstall isAdmin={!access.readOnly} onFinished={() => cli.load(true)} />
	<div class="pv-acts"><button type="button" class="st-btn ghost" onclick={cancel}>Cancel</button></div>
{:else if signedIn}
	<div class="st-card pv-card">
		<div class="pv-flow">
			<span class="status" role="status"
				><StatusDot tone="ok" />CLI signed in{cli.status.email ? ` as ${cli.status.email}` : ''}</span
			>
		</div>
	</div>
	{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn primary" disabled={finishing} onclick={done}>Done</button>
	</div>
{:else if cli.terminalOpen}
	<RowGroup title="Sign in" bare>
		<ClaudeCliTerminal {cli} />
	</RowGroup>
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" onclick={cancel}>Cancel</button>
	</div>
{:else}
	<RowGroup title="Sign in">
		{#if !cli.status.installed}
			<SettingRow label="Claude Code CLI" hint="Not installed on this worker">
				<button type="button" class="st-btn" disabled={cli.checking} onclick={() => cli.load(true)}
					><RefreshCw />Check again</button
				>
			</SettingRow>
		{:else if canUseTerminal}
			<SettingRow label="Claude Code CLI" hint="Your own Claude account">
				<button type="button" class="st-btn primary" onclick={() => cli.startLogin()}
					><SquareTerminal />Start sign-in</button
				>
			</SettingRow>
		{/if}
		{#if cli.status.installed}
			<SettingRow
				label={canUseTerminal ? 'Or on the host' : 'Run on the host'}
				hint={cli.status.login.surface === 'docker' ? 'Unraid: the Terminal button' : undefined}
			>
				<button type="button" class="st-btn" disabled={cli.checking} onclick={() => cli.load(true)}
					><RefreshCw />Check again</button
				>
			</SettingRow>
			<div class="cmd"><CodeBlock code={cli.hostCommand} prompt /></div>
		{/if}
	</RowGroup>
	{#if cli.loginError}<p class="pv-msg err" role="alert">{cli.loginError}</p>{/if}
	<div class="pv-acts">
		<button type="button" class="st-btn ghost" onclick={cancel}>Cancel</button>
	</div>
{/if}

<style>
	.status {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		font-size: 13px;
	}
	.cmd {
		padding: 0 16px 14px;
	}
</style>
