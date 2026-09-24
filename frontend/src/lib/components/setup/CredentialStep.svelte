<script lang="ts">
	/**
	 * Wizard step: the credential for the chosen mode.
	 *
	 * - Subscription: the Claude Code CLI panel. On Docker it offers the in-app
	 *   terminal that runs `claude auth login`; on the desktop app it shows the
	 *   command to run in the user's own terminal (the worker reports which).
	 *   Either way "Check again" re-reads `claude auth status`.
	 * - API key: the key form. The worker checks the key with Anthropic before
	 *   saving it, and keeps it on this worker only.
	 *
	 * "Continue" stays disabled until agents can start with this mode: the
	 * blocking empty state says what is missing. There is no way to finish
	 * setup without a credential.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { AlertTriangle, Check, ExternalLink, Loader2, RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		getAgentCredentials, getAnthropicKeyState, keyOverridesSubscription, saveAnthropicApiKey,
		type AgentCredentials, type AnthropicKeyState, type AuthModeState, type ClaudeAuthMode
	} from '$lib/api/agent-credentials';
	import ClaudeCliPanel from '$lib/components/settings/ClaudeCliPanel.svelte';

	interface Props {
		mode: ClaudeAuthMode;
		modeState: AuthModeState;
		isAdmin: boolean;
		onContinue: () => void;
		onBack?: () => void;
		/** Go back to the mode step with the other mode picked. */
		onSwitchMode?: (mode: ClaudeAuthMode) => void;
		/** How often to re-check while waiting, in ms. */
		pollMs?: number;
	}
	let { mode, modeState, isAdmin, onContinue, onBack, onSwitchMode, pollMs = 5000 }: Props = $props();

	let readiness = $state<AgentCredentials | null>(null);
	let checking = $state(false);
	let checkError = $state('');
	let key = $state<AnthropicKeyState | null>(null);
	let keyInput = $state('');
	let replacing = $state(false);
	let saving = $state(false);
	let saveError = $state('');
	let timer: ReturnType<typeof setInterval> | undefined;
	let disposed = false;

	const ready = $derived(readiness?.ready === true);
	const keyWins = $derived(mode === 'subscription' && keyOverridesSubscription(modeState, key));
	const CONSOLE_KEYS = 'https://console.anthropic.com/settings/keys';

	async function check() {
		if (checking) return;
		checking = true;
		try {
			const [next, keyState] = await Promise.all([
				getAgentCredentials(mode),
				getAnthropicKeyState().catch(() => null)
			]);
			if (disposed) return;
			readiness = next; key = keyState; checkError = '';
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
			key = await saveAnthropicApiKey(value);
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
			<ClaudeCliPanel variant="setup" {isAdmin} onStatus={() => void check()}
				onUseApiKey={onSwitchMode ? () => onSwitchMode('api_key') : undefined} />
		</div>
		{#if keyWins}
			<div role="status" class="bg-warning/10 border border-warning/30 text-foreground px-3 py-2 rounded-lg text-sm">
				An Anthropic API key is saved on this worker. This version of AI Shuffle uses a saved key instead of the CLI's sign-in, so runs are billed to the key. Remove it in Settings > Providers to use your subscription.
			</div>
		{/if}
	{:else}
		{#if key?.set && !replacing}
			<div class="bg-muted/30 border border-border rounded-lg px-4 py-3 flex items-center justify-between gap-3">
				<div class="min-w-0">
					<p class="text-sm text-foreground">Anthropic API key saved</p>
					{#if key.masked}<p class="text-xs text-muted-foreground font-mono truncate">{key.masked}</p>{/if}
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
			<span>{mode === 'subscription' ? 'The Claude Code CLI is signed in. Agents can start.' : 'Your API key is saved. Agents can start.'}</span>
		</div>
	{:else if readiness || checkError}
		<!-- Blocking empty state: nothing runs until the credential exists. -->
		<div role="alert" class="bg-destructive/10 border border-destructive/40 px-4 py-3 rounded-lg text-sm" data-testid="setup-credential-missing">
			<p class="flex items-center gap-2 font-medium text-foreground"><AlertTriangle size={16} strokeWidth={2} class="text-destructive" /> Agents cannot start yet</p>
			<p class="text-muted-foreground mt-1">
				{checkError || readiness?.reason || 'No credential is set up for this mode.'}
				{mode === 'subscription' ? 'Sign in above, then choose Check again.' : 'Save a key above.'}
				AI Shuffle never falls back to anyone else's account.
			</p>
		</div>
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		{#if !ready}
			<button type="button" onclick={() => void check()} class="btn btn-secondary flex-1 py-3" disabled={checking}>
				{#if checking}<Loader2 size={16} class="inline-block animate-spin mr-2" />{:else}<RefreshCw size={16} class="inline-block mr-2" />{/if}Check again
			</button>
		{/if}
		<button type="button" onclick={onContinue} class="btn btn-primary flex-1 py-3" disabled={!ready}>Continue</button>
	</div>
	<p class="text-xs text-muted-foreground text-center">OpenAI, xAI, OpenRouter and fal.ai can be added in Settings > Providers after setup.</p>
</div>
