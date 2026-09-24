<script lang="ts">
	/**
	 * Wizard step: how Claude Code is paid for on this worker. Subscription
	 * (the stock Claude Code CLI, signed in by the owner) or an Anthropic API
	 * key, with the advice to use a key for anything that runs unattended.
	 * The choice is stored through `$lib/api/agent-credentials`; on a worker
	 * without the mode setting it is kept for this visit only.
	 */
	import { untrack } from 'svelte';
	import { ExternalLink, KeyRound, Loader2, SquareTerminal } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { setClaudeAuthMode, type AuthModeState, type ClaudeAuthMode } from '$lib/api/agent-credentials';

	interface Props {
		/** The current choice, if any. */
		mode: ClaudeAuthMode | null;
		onChosen: (state: AuthModeState) => void;
		onBack?: () => void;
	}
	let { mode, onChosen, onBack }: Props = $props();

	let selected = $state<ClaudeAuthMode | null>(untrack(() => mode));
	let saving = $state(false);
	let error = $state('');

	const MODES_DOC = 'https://hobbycoders.com/docs/getting-started/modes-and-costs';

	async function save() {
		if (!selected || saving) return;
		saving = true; error = '';
		try {
			onChosen(await setClaudeAuthMode(selected));
		} catch (e) {
			error = getErrorMessage(e, 'The choice could not be saved. Try again.');
		} finally {
			saving = false;
		}
	}
</script>

<div class="space-y-4" data-testid="setup-mode">
	<div role="radiogroup" aria-label="How Claude Code is paid for" class="space-y-3">
		<label class="option" class:selected={selected === 'subscription'}>
			<input type="radio" name="claude-mode" value="subscription" bind:group={selected} class="sr-only" />
			<SquareTerminal size={20} strokeWidth={1.75} class="shrink-0 text-primary mt-0.5" />
			<span>
				<span class="title">Claude subscription</span>
				<span class="body">Your Pro or Max plan, billed by Anthropic. You sign in to the standard Claude Code CLI yourself, on Anthropic's own page. Your plan's usage limits apply. Best for hands-on work while you are at the keyboard.</span>
			</span>
		</label>
		<label class="option" class:selected={selected === 'api_key'}>
			<input type="radio" name="claude-mode" value="api_key" bind:group={selected} class="sr-only" />
			<KeyRound size={20} strokeWidth={1.75} class="shrink-0 text-primary mt-0.5" />
			<span>
				<span class="title">API key</span>
				<span class="body">Your own Anthropic Console key, billed per use by Anthropic. Best for scheduled jobs, overnight runs, connectors and many agents at once.</span>
			</span>
		</label>
	</div>

	<div class="bg-primary/10 border border-primary/30 rounded-lg p-4 text-sm" data-testid="setup-mode-guidance">
		<p class="font-medium text-foreground mb-1">Use an API key for scheduled and unattended runs</p>
		<p class="text-muted-foreground">
			Automations, wake-ups, connectors and background agents run without you. On a subscription they can use up your plan's limits quickly, and running them is at your own risk under Anthropic's terms.
			<a href={MODES_DOC} target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1 text-primary underline">Modes and costs <ExternalLink size={12} /></a>
		</p>
	</div>

	<p class="text-xs text-muted-foreground">You can change the mode later in Settings. AI Shuffle never switches between them on its own, and never falls back to anyone else's account.</p>

	{#if error}<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{error}</div>{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		<button type="button" onclick={save} class="btn btn-primary flex-1 py-3" disabled={!selected || saving}>
			{#if saving}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Continue
		</button>
	</div>
</div>

<style>
	.option {
		display: flex;
		gap: 0.75rem;
		padding: 0.875rem 1rem;
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		cursor: pointer;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
	}
	.option:focus-within { outline: 2px solid var(--ring); outline-offset: 2px; }
	.option.selected { border-color: var(--primary); background: color-mix(in srgb, var(--primary) 10%, transparent); }
	.title { display: block; font-size: 0.9375rem; font-weight: 600; color: var(--foreground); margin-bottom: 0.25rem; }
	.body { display: block; font-size: 0.8125rem; line-height: 1.5; color: var(--muted-foreground); }
</style>
