<script lang="ts">
	/**
	 * The Claude choice inside setup's "AI provider" step. Claude is one
	 * provider among the others; this is its own connect flow.
	 *
	 * Two ways to pay, picked here (nothing is pre-selected):
	 * - Subscription: the stock Claude Code CLI, signed in by the owner on
	 *   Anthropic's page. `ClaudeCliPanel variant="setup"` keeps the Docker
	 *   (in-app terminal) and desktop (own terminal) differences.
	 * - API key: an Anthropic Console key. The worker checks it first and the
	 *   save also sets Claude to API key mode (`use_for_claude_code`).
	 *
	 * Picking one stores the Claude mode (`PUT /settings/claude-auth`), the same
	 * setting as Settings > Providers > Claude. Readiness comes from the worker's
	 * `claude-auth` summary; `onReady` fires once the chosen way has its credential.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { Check, ExternalLink, KeyRound, Loader2, SquareTerminal } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { claudePlanLabel } from '$lib/api/claude-cli';
	import {
		getAgentCredentials, keyLabel, saveAnthropicApiKey, setClaudeAuthMode,
		type AgentCredentials, type ClaudeAuthMode
	} from '$lib/api/agent-credentials';
	import ClaudeCliPanel from '$lib/components/settings/ClaudeCliPanel.svelte';

	type Way = Exclude<ClaudeAuthMode, 'off'>;

	interface Props {
		isAdmin: boolean;
		/** The chosen way's credential exists. */
		onReady?: () => void;
		/** How often to re-check while waiting, in ms. */
		pollMs?: number;
	}
	let { isAdmin, onReady, pollMs = 5000 }: Props = $props();

	let creds = $state<AgentCredentials | null>(null);
	let way = $state<Way | null>(null);
	let switching = $state(false);
	let checking = $state(false);
	let error = $state('');
	let keyInput = $state('');
	let replacing = $state(false);
	let saving = $state(false);
	let saveError = $state('');
	let timer: ReturnType<typeof setInterval> | undefined;
	let disposed = false;
	let announced = false;

	const ready = $derived(!!way && creds?.mode === way && creds.credentialReady);
	const keySet = $derived(creds?.apiKey.set === true);
	const account = $derived.by(() => {
		if (!creds?.cli.signed_in) return '';
		const plan = claudePlanLabel(creds.cli.plan);
		const who = creds.cli.email ?? '';
		if (who && plan) return who + ' (' + plan + ')';
		return who || (plan ? plan + ' plan' : '');
	});
	const readyText = $derived.by(() => {
		if (way === 'subscription') return account ? 'Claude is connected with the subscription of ' + account + '.' : 'Claude is connected with your subscription.';
		const last4 = creds?.apiKey.last4;
		return last4 ? 'Claude is connected with your Anthropic API key (ending in ' + last4 + ').' : 'Claude is connected with your Anthropic API key.';
	});
	const CONSOLE_KEYS = 'https://console.anthropic.com/settings/keys';
	const MODES_DOC = 'https://hobbycoders.com/docs/getting-started/modes-and-costs';

	$effect(() => {
		if (ready && !announced) { announced = true; onReady?.(); }
		if (!ready) announced = false;
	});

	async function check(refresh = false) {
		if (checking) return;
		checking = true;
		try {
			const next = await getAgentCredentials(refresh);
			if (disposed) return;
			creds = next; error = '';
			// A returning owner whose Claude already works starts on that way.
			if (!way && next.credentialReady && next.mode !== 'off') way = next.mode;
		} catch (e) {
			if (!disposed) error = getErrorMessage(e, 'Could not ask the worker about Claude.');
		} finally {
			checking = false;
		}
	}

	async function choose(next: Way) {
		if (switching) return;
		way = next; error = '';
		if (creds?.mode === next) return;
		switching = true;
		try {
			creds = await setClaudeAuthMode(next);
		} catch (e) {
			error = getErrorMessage(e, 'The choice could not be saved. Try again.');
		} finally {
			switching = false;
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
		timer = setInterval(() => { if (way && !ready) void check(); }, pollMs);
	});
	onDestroy(() => { disposed = true; clearInterval(timer); });
</script>

<div class="space-y-4" data-testid="setup-claude">
	<div role="radiogroup" aria-label="How to pay for Claude" class="ways">
		<label class="way" class:selected={way === 'subscription'}>
			<input type="radio" name="claude-way" value="subscription" checked={way === 'subscription'}
				onchange={() => choose('subscription')} class="sr-only" disabled={switching} />
			<SquareTerminal size={18} strokeWidth={1.75} class="shrink-0 text-primary mt-0.5" />
			<span>
				<span class="title">Subscription</span>
				<span class="body">Your Claude Pro or Max plan. You sign in to the Claude Code CLI yourself, on Anthropic's page.</span>
			</span>
		</label>
		<label class="way" class:selected={way === 'api_key'}>
			<input type="radio" name="claude-way" value="api_key" checked={way === 'api_key'}
				onchange={() => choose('api_key')} class="sr-only" disabled={switching} />
			<KeyRound size={18} strokeWidth={1.75} class="shrink-0 text-primary mt-0.5" />
			<span>
				<span class="title">API key</span>
				<span class="body">An Anthropic Console key, billed per use by Anthropic.</span>
			</span>
		</label>
	</div>

	{#if way === 'subscription'}
		<div class="bg-warning/10 border border-warning/30 rounded-lg px-3 py-2.5 text-xs text-muted-foreground" data-testid="setup-claude-unattended">
			<p class="font-medium text-foreground mb-0.5">Use an API key for unattended runs</p>
			Automations, wake-ups and background agents run without you. On a subscription they can use up your plan's limits quickly, and running them is at your own risk under Anthropic's terms.
			<a href={MODES_DOC} target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 text-primary underline">Modes and costs <ExternalLink size={11} /></a>
		</div>
		<div class="bg-muted/30 border border-border rounded-lg py-2">
			<ClaudeCliPanel variant="setup" {isAdmin} onStatus={() => void check(true)} onUseApiKey={() => choose('api_key')} />
		</div>
	{:else if way === 'api_key'}
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
				<p class="text-sm text-muted-foreground">
					Create a key in the <a href={CONSOLE_KEYS} target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 text-primary underline">Anthropic Console <ExternalLink size={12} /></a> and paste it here. AI Shuffle checks it with Anthropic before saving.
				</p>
				<div>
					<label for="setup-api-key" class="block text-xs text-muted-foreground mb-1">Anthropic API key</label>
					<input id="setup-api-key" type="password" class="input font-mono text-sm" autocomplete="off" spellcheck="false"
						placeholder="sk-ant-…" bind:value={keyInput} disabled={saving} />
				</div>
				{#if saveError}<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{saveError}</div>{/if}
				<div class="flex gap-3">
					{#if replacing}<button type="button" class="btn btn-secondary flex-1 py-2.5" onclick={() => { replacing = false; keyInput = ''; }} disabled={saving}>Keep the saved key</button>{/if}
					<button type="submit" class="btn btn-primary flex-1 py-2.5" disabled={saving || !keyInput.trim()}>
						{#if saving}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Save key
					</button>
				</div>
				<p class="text-xs text-muted-foreground">The key is encrypted and stays on this worker. It is never sent to HobbyCoders.</p>
			</form>
		{/if}
	{/if}

	{#if error}<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{error}</div>{/if}

	{#if ready}
		<div role="status" class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2" data-testid="setup-claude-ready">
			<Check size={16} strokeWidth={2} />
			<span>{readyText}</span>
		</div>
	{/if}
</div>

<style>
	.ways { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; }
	@media (max-width: 420px) { .ways { grid-template-columns: 1fr; } }
	.way {
		display: flex;
		gap: 0.625rem;
		padding: 0.75rem 0.875rem;
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		cursor: pointer;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
		transition: border-color 0.18s ease-out, background-color 0.18s ease-out;
	}
	.way:hover { border-color: color-mix(in srgb, var(--primary) 45%, var(--border)); }
	.way:focus-within { outline: 2px solid var(--ring); outline-offset: 2px; }
	.way.selected { border-color: var(--primary); background: color-mix(in srgb, var(--primary) 10%, transparent); }
	.title { display: block; font-size: 0.875rem; font-weight: 600; color: var(--foreground); margin-bottom: 0.125rem; }
	.body { display: block; font-size: 0.75rem; line-height: 1.45; color: var(--muted-foreground); }
	@media (prefers-reduced-motion: reduce) { .way { transition: none; } }
</style>
