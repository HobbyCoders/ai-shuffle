<script lang="ts">
	/**
	 * ClaudeAuthModePanel — which credential Claude Code runs on.
	 *
	 * Two modes, one active at a time, chosen by the owner:
	 *  - Claude subscription: the stock CLI's own sign-in (see ClaudeCliPanel).
	 *  - API key: the Anthropic API key stored on this worker.
	 * Shows the active mode with the CLI account or the key's last four
	 * characters, and never switches on its own. Used by Settings → Providers.
	 */
	import { onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { claudePlanLabel } from '$lib/api/claude-cli';
	import {
		getClaudeAuth,
		setClaudeAuthMode,
		type ClaudeAuthMode,
		type ClaudeAuthSummary
	} from '$lib/api/claude-auth';
	import { auth } from '$lib/stores/auth.svelte';

	interface Props {
		isAdmin?: boolean;
	}

	let { isAdmin = false }: Props = $props();

	let summary = $state<ClaudeAuthSummary | null>(null);
	let loadError = $state('');
	let saving = $state(false);
	let saveError = $state('');

	const planLabel = $derived(claudePlanLabel(summary?.cli.plan));
	const activeReady = $derived(
		summary ? (summary.mode === 'subscription' ? summary.cli.signed_in : summary.api_key.set) : true
	);

	function subscriptionDetail(s: ClaudeAuthSummary): string {
		if (!s.cli.installed) return 'Claude Code CLI not installed';
		if (!s.cli.signed_in) return 'CLI not signed in';
		const who = s.cli.email ?? 'Signed in';
		return planLabel ? `${who} (${planLabel})` : who;
	}

	function keyDetail(s: ClaudeAuthSummary): string {
		if (!s.api_key.set) return 'No key stored';
		return s.api_key.last4 ? `Key ending in ${s.api_key.last4}` : 'Key stored';
	}

	async function load(refresh = false) {
		try {
			summary = await getClaudeAuth(refresh);
			loadError = '';
		} catch (e) {
			loadError = getErrorMessage(e, 'Could not read the Claude Code mode.');
		}
	}

	async function choose(mode: ClaudeAuthMode) {
		if (!summary || saving || summary.mode === mode) return;
		saving = true;
		saveError = '';
		try {
			summary = await setClaudeAuthMode(mode);
			auth.checkAuth().catch(() => {});
		} catch (e) {
			saveError = getErrorMessage(e, 'Could not change the mode.');
		} finally {
			saving = false;
		}
	}

	onMount(() => {
		void load();
	});
</script>

<div class="mode-panel" data-testid="claude-auth-mode-panel">
	{#if !summary}
		<p class="hint">{loadError || 'Loading…'}</p>
	{:else}
		<div class="options" role="radiogroup" aria-label="Claude Code mode">
			<label class="opt" class:active={summary.mode === 'subscription'}>
				<input
					type="radio"
					name="claude-auth-mode"
					value="subscription"
					checked={summary.mode === 'subscription'}
					disabled={!isAdmin || saving}
					onchange={() => choose('subscription')}
				/>
				<span class="opt-text">
					<span class="opt-title">Claude subscription</span>
					<span class="opt-detail" data-testid="claude-auth-subscription-detail">{subscriptionDetail(summary)}</span>
				</span>
			</label>
			<label class="opt" class:active={summary.mode === 'api_key'}>
				<input
					type="radio"
					name="claude-auth-mode"
					value="api_key"
					checked={summary.mode === 'api_key'}
					disabled={!isAdmin || saving}
					onchange={() => choose('api_key')}
				/>
				<span class="opt-text">
					<span class="opt-title">Anthropic API key</span>
					<span class="opt-detail" data-testid="claude-auth-key-detail">{keyDetail(summary)}</span>
				</span>
			</label>
		</div>

		<p class="hint pad" data-testid="claude-auth-active">
			Active: <strong>{summary.mode_label}</strong>.
			{#if summary.mode === 'subscription'}
				Agents run on the plan of the account the CLI is signed in to. A stored API key is not used in this mode.
			{:else}
				Agents run on your API key, billed per use by Anthropic. The CLI's own sign-in is not used in this mode.
			{/if}
			AI Shuffle never switches modes on its own.
		</p>
		{#if !activeReady}
			<p class="msg warn pad" role="status" data-testid="claude-auth-missing">
				{#if summary.mode === 'subscription'}
					The Claude Code CLI is not signed in, so Claude agents cannot start. Sign in below, or choose API key mode.
				{:else}
					No API key is stored, so Claude agents cannot start. Add a key under Providers, or choose subscription mode.
				{/if}
			</p>
		{/if}
		{#if saveError}<p class="msg err pad" role="alert">{saveError}</p>{/if}
		{#if !isAdmin}<p class="hint pad">Only the owner can change the mode.</p>{/if}
	{/if}
</div>

<style>
	.mode-panel { display: grid; gap: 8px; padding: 4px 0; }
	.options { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; padding: 0 14px; }
	.opt { display: flex; gap: 10px; align-items: flex-start; padding: 10px; border: 1px solid var(--border); border-radius: 8px; cursor: pointer; }
	.opt.active { border-color: color-mix(in oklch, var(--primary) 55%, var(--border)); background: color-mix(in oklch, var(--primary) 6%, transparent); }
	.opt input { margin-top: 2px; }
	.opt-text { display: grid; gap: 2px; min-width: 0; }
	.opt-title { font-size: 13px; font-weight: 600; }
	.opt-detail { font-size: 12px; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.hint { margin: 0; color: var(--muted-foreground); font-size: 12px; line-height: 1.5; }
	.pad { padding: 0 14px; }
	.msg { margin: 0; font-size: 12.5px; }
	.msg.err { color: var(--destructive); }
	.msg.warn { color: var(--warning, #d97706); }
</style>
