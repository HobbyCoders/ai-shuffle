<script lang="ts">
	/**
	 * Wizard step: the credential for the chosen mode.
	 *
	 * - Subscription: the Claude Code CLI panel. On Docker it offers the in-app
	 *   terminal that runs `claude auth login`; on the desktop app it shows the
	 *   command to run in the user's own terminal (the worker reports which).
	 *   Either way "Check again" re-reads `claude auth status`.
	 * - API key: the key form. The worker checks the key with Anthropic before
	 *   saving it, and keeps it on this worker only. The save also sets Claude
	 *   Code to API key mode (`use_for_claude_code`).
	 *
	 * Everything shown comes from one call to the worker's credential gate
	 * (`GET /settings/claude-auth`): the mode, the CLI account or the key's last
	 * four, and why agents cannot start. "Continue" stays disabled until the
	 * mode's credential exists: the blocking empty state shows the gate's
	 * reason. There is no way to finish setup without a credential.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { AlertTriangle, Check, ExternalLink, Loader2, RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { claudePlanLabel } from '$lib/api/claude-cli';
	import {
		getAgentCredentials, keyLabel, saveAnthropicApiKey,
		type AgentCredentials, type ClaudeAuthMode
	} from '$lib/api/agent-credentials';
	import ClaudeCliPanel from '$lib/components/settings/ClaudeCliPanel.svelte';

	interface Props {
		mode: ClaudeAuthMode;
		isAdmin: boolean;
		onContinue: () => void;
		onBack?: () => void;
		/** Go back to the mode step with the other mode picked. */
		onSwitchMode?: (mode: ClaudeAuthMode) => void;
		/** How often to re-check while waiting, in ms. */
		pollMs?: number;
	}
	let { mode, isAdmin, onContinue, onBack, onSwitchMode, pollMs = 5000 }: Props = $props();

	let creds = $state<AgentCredentials | null>(null);
	let checking = $state(false);
	let checkError = $state('');
	let keyInput = $state('');
	let replacing = $state(false);
	let saving = $state(false);
	let saveError = $state('');
	let timer: ReturnType<typeof setInterval> | undefined;
	let disposed = false;

	/** The worker runs in the mode this step is for, and has its credential. */
	const ready = $derived(creds?.mode === mode && creds.credentialReady);
	const keySet = $derived(creds?.apiKey.set === true);
	const account = $derived.by(() => {
		if (!creds?.cli.signed_in) return '';
		const plan = claudePlanLabel(creds.cli.plan);
		const who = creds.cli.email ?? '';
		if (who && plan) return who + ' (' + plan + ')';
		return who || (plan ? plan + ' plan' : '');
	});
	const readyText = $derived.by(() => {
		if (mode === 'subscription') return account ? 'The Claude Code CLI is signed in as ' + account + '.' : 'The Claude Code CLI is signed in.';
		const last4 = creds?.apiKey.last4;
		return last4 ? 'Claude Code runs on your Anthropic API key (ending in ' + last4 + ').' : 'Claude Code runs on your Anthropic API key.';
	});
	const missingReason = $derived.by(() => {
		if (checkError) return checkError;
		if (!creds) return '';
		if (creds.mode !== mode) return 'Claude Code is set to ' + creds.modeLabel + ' mode on this worker. Go back and choose the mode again.';
		// With the license blocking too, the gate's reason names the license instead.
		if (creds.licenseAllowsRuns && creds.reason) return creds.reason;
		return mode === 'subscription' ? 'The Claude Code CLI is not signed in.' : 'No Anthropic API key is stored on this worker.';
	});
	const CONSOLE_KEYS = 'https://console.anthropic.com/settings/keys';

	async function check(refresh = false) {
		if (checking) return;
		checking = true;
		try {
			const next = await getAgentCredentials(refresh);
			if (disposed) return;
			creds = next; checkError = '';
		} catch (e) {
			if (!disposed) checkError = getErrorMessage(e, 'Could not ask the worker whether agents can start.');
		} finally {
			checking = false;
		}
	}

	async function saveKey(event: SubmitEvent) {
		event.preventDefault();
		const value = keyInput.trim();
		if (!value || saving) return;
		saving = true; saveError = '';
		try {
			await saveAnthropicApiKey(value, { useForClaudeCode: true });
			keyInput = ''; replacing = false;
			await check();
		} catch (e) {
			saveError = getErrorMessage(e, 'The key could not be saved.');
		} finally {
			saving = false;
		}
	}

	onMount(() => {
		void check();
		// A sign-in finished in another terminal shows up without a click.
		timer = setInterval(() => { if (!ready) void check(); }, pollMs);
	});
	onDestroy(() => { disposed = true; clearInterval(timer); });
</script>

<div class="space-y-4" data-testid="setup-credential">
	{#if mode === 'subscription'}
		<p class="text-sm text-muted-foreground">
			AI Shuffle runs the standard Claude Code CLI. You sign in to it yourself on Anthropic's own page; AI Shuffle never sees, copies or stores your Claude sign-in.
		</p>
		<div class="bg-muted/30 border border-border rounded-lg py-2">
			<ClaudeCliPanel variant="setup" {isAdmin} onStatus={() => void check(true)}
				onUseApiKey={onSwitchMode ? () => onSwitchMode('api_key') : undefined} />
		</div>
		{#if keySet && creds}
			<p class="text-xs text-muted-foreground" data-testid="setup-credential-key-unused">
				An Anthropic API key is also stored on this worker ({keyLabel(creds.apiKey)}). It is not used in subscription mode.
			</p>
		{/if}
	{:else}
		{#if keySet && creds && !replacing}
			<div class="bg-muted/30 border border-border rounded-lg px-4 py-3 flex items-center justify-between gap-3">
				<div class="min-w-0">
					<p class="text-sm text-foreground">Anthropic API key saved</p>
					<p class="text-xs text-muted-foreground font-mono truncate">{keyLabel(creds.apiKey)}</p>
				</div>
				<button type="button" class="btn btn-secondary text-sm" onclick={() => (replacing = true)}>Replace</button>
			</div>
		{:else}
			<form class="space-y-3" onsubmit={saveKey}>
				<ol class="text-sm text-muted-foreground list-decimal pl-5 space-y-1">
					<li>Create a key in the <a href={CONSOLE_KEYS} target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 text-primary underline">Anthropic Console <ExternalLink size={12} /></a>, under API keys. Set a monthly spend limit there while you are at it.</li>
					<li>Paste it here and choose Save. AI Shuffle checks it with Anthropic first.</li>
				</ol>
				<div>
					<label for="setup-api-key" class="block text-xs text-muted-foreground mb-1">Anthropic API key</label>
					<input id="setup-api-key" type="password" class="input font-mono text-sm" autocomplete="off" spellcheck="false"
						placeholder="sk-ant-…" bind:value={keyInput} disabled={saving} />
				</div>
				{#if saveError}<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{saveError}</div>{/if}
				<div class="flex gap-3">
					{#if replacing}<button type="button" class="btn btn-secondary flex-1 py-2.5" onclick={() => { replacing = false; keyInput = ''; }} disabled={saving}>Keep the saved key</button>{/if}
					<button type="submit" class="btn btn-primary flex-1 py-2.5" disabled={saving || !keyInput.trim()}>
						{#if saving}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Save
					</button>
				</div>
				<p class="text-xs text-muted-foreground">The key is encrypted and stays on this worker. It is not copied to your other workers, and it is never sent to HobbyCoders.</p>
			</form>
		{/if}
		{#if onSwitchMode}
			<p class="text-xs text-muted-foreground">Prefer your Claude subscription? <button type="button" class="text-primary underline" onclick={() => onSwitchMode('subscription')}>Sign in to the Claude Code CLI instead</button></p>
		{/if}
	{/if}

	{#if ready}
		<div role="status" class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2" data-testid="setup-credential-ready">
			<Check size={16} strokeWidth={2} />
			<span>{readyText}{creds?.ready ? ' Agents can start.' : ''}</span>
		</div>
		{#if creds && !creds.licenseAllowsRuns && creds.reason}
			<p role="status" class="bg-warning/10 border border-warning/30 text-foreground px-3 py-2 rounded-lg text-sm" data-testid="setup-credential-license">{creds.reason}</p>
		{/if}
	{:else if creds || checkError}
		<!-- Blocking empty state: nothing runs until the credential exists. -->
		<div role="alert" class="bg-destructive/10 border border-destructive/40 px-4 py-3 rounded-lg text-sm" data-testid="setup-credential-missing">
			<p class="flex items-center gap-2 font-medium text-foreground"><AlertTriangle size={16} strokeWidth={2} class="text-destructive" /> Agents cannot start yet</p>
			<p class="text-muted-foreground mt-1" data-testid="setup-credential-reason">{missingReason}</p>
			<p class="text-muted-foreground mt-1">{mode === 'subscription' ? 'Sign in above, then choose Check again.' : 'Save a key above.'}</p>
		</div>
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		{#if !ready}
			<button type="button" onclick={() => void check(true)} class="btn btn-secondary flex-1 py-3" disabled={checking}>
				{#if checking}<Loader2 size={16} class="inline-block animate-spin mr-2" />{:else}<RefreshCw size={16} class="inline-block mr-2" />{/if}Check again
			</button>
		{/if}
		<button type="button" onclick={onContinue} class="btn btn-primary flex-1 py-3" disabled={!ready}>Continue</button>
	</div>
	<p class="text-xs text-muted-foreground text-center">Add OpenAI, xAI, OpenRouter, fal.ai or your own gateway later in Settings > Providers.</p>
</div>
