<script lang="ts">
	/**
	 * Wizard step: the first agent run. One short prompt in a new chat session
	 * (no project, no file tools), with the reply streamed in, so the owner
	 * sees an agent answer before leaving setup. It names what pays for the
	 * run: the CLI's account on a subscription, or the saved key's last four,
	 * both from the worker's `claude-auth` summary.
	 *
	 * A turn the credential gate refuses (`agent_setup_required`) or the loop
	 * guard holds (`loop_guard_confirm`) shows the worker's own message, with
	 * a way back to the sign-in step for the first.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { AlertTriangle, Check, Loader2, Play } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { getAgentCredentials, type AgentCredentials, type ClaudeAuthMode } from '$lib/api/agent-credentials';
	import {
		ensureStarterAgent, FIRST_RUN_PROMPT, LOOP_GUARD_CODE, SETUP_REQUIRED_CODE, startFirstAgentRun,
		type FirstRunHandle, type FirstRunState
	} from '$lib/services/first-agent-run';

	interface Props {
		mode: ClaudeAuthMode | null;
		onContinue: () => void;
		onBack?: () => void;
		/** Go to the sign-in step, after the worker refused the run for setup. */
		onFixCredential?: () => void;
	}
	let { mode, onContinue, onBack, onFixCredential }: Props = $props();

	let run = $state<FirstRunState | null>(null);
	let starting = $state(false);
	let startError = $state('');
	let creds = $state<AgentCredentials | null>(null);
	let handle: FirstRunHandle | null = null;
	let outputEl = $state<HTMLDivElement | null>(null);

	const running = $derived(starting || (!!run && ['starting', 'waiting', 'streaming'].includes(run.phase)));
	const finished = $derived(!!run && ['done', 'failed', 'needs-input'].includes(run.phase));
	const succeeded = $derived(run?.phase === 'done');
	const attempted = $derived(finished || !!startError);
	const cliAccount = $derived(creds?.cli.signed_in ? creds.cli.email : null);
	const keyLast4 = $derived(creds?.apiKey.last4 ?? null);
	const refused = $derived(finished && run?.code === SETUP_REQUIRED_CODE);
	const held = $derived(finished && run?.code === LOOP_GUARD_CODE);

	// The worker's own mode when this visit has no choice yet (a reload past the mode step).
	const payingMode = $derived(mode ?? creds?.mode ?? null);
	const payer = $derived(payingMode === 'api_key'
		? `your Anthropic API key${keyLast4 ? ` (ending in ${keyLast4})` : ''}, billed per use`
		: payingMode === 'subscription'
			? `your Claude plan${cliAccount ? ` (${cliAccount})` : ''}, through the Claude Code CLI`
			: 'the credential you set up');

	async function start() {
		if (running) return;
		starting = true; startError = ''; run = null;
		handle?.cancel();
		try {
			const agent = await ensureStarterAgent();
			handle = startFirstAgentRun({
				profileId: agent.id,
				onUpdate: (state) => {
					run = state;
					queueMicrotask(() => { if (outputEl) outputEl.scrollTop = outputEl.scrollHeight; });
				}
			});
			starting = false;
			await handle.done;
		} catch (e) {
			startError = getErrorMessage(e, 'The run could not start.');
		} finally {
			starting = false;
		}
	}

	onMount(() => {
		getAgentCredentials().then((c) => { creds = c; }).catch(() => {});
	});
	onDestroy(() => { handle?.cancel(); });
</script>

<div class="space-y-4" data-testid="setup-first-run">
	<p class="text-sm text-muted-foreground">
		Run a one-line task to see an agent answer. It runs in a new chat session with no project and no access to your files, and it is paid for by {payer}.
	</p>
	<div class="bg-muted/30 border border-border rounded-lg px-4 py-3">
		<p class="text-xs text-muted-foreground mb-1">The task</p>
		<p class="text-sm text-foreground">{FIRST_RUN_PROMPT}</p>
	</div>

	{#if run && (run.text || running)}
		<div bind:this={outputEl} class="reply bg-background border border-border rounded-lg px-4 py-3 text-sm text-foreground" aria-live="polite" aria-label="Agent reply" data-testid="setup-first-run-reply">
			{#if run.text}{run.text}{:else}<span class="text-muted-foreground flex items-center gap-2"><Loader2 size={14} class="animate-spin" /> Waiting for the agent…</span>{/if}
		</div>
	{/if}

	{#if succeeded}
		<div role="status" class="bg-success/10 border border-success/30 text-success px-3 py-2.5 rounded-lg text-sm flex items-center gap-2">
			<Check size={16} strokeWidth={2} /><span>It works. The session is in your sidebar.</span>
		</div>
	{:else if (finished && run?.error) || startError}
		<div role="alert" class="bg-destructive/10 border border-destructive/40 px-4 py-3 rounded-lg text-sm" data-testid="setup-first-run-error" data-code={run?.code ?? ''}>
			<p class="flex items-center gap-2 font-medium text-foreground">
				<AlertTriangle size={16} strokeWidth={2} class="text-destructive" />
				{refused ? 'Agents cannot start yet' : held ? 'The run is waiting for your confirmation' : 'The run did not finish'}
			</p>
			<p class="text-muted-foreground mt-1">{startError || run?.error}</p>
			{#if refused && onFixCredential}
				<p class="text-muted-foreground mt-1"><button type="button" class="text-primary underline" onclick={onFixCredential}>Go back to Sign in</button> to finish setting up the credential, then try again.</p>
			{:else if !refused && !held}
				<p class="text-muted-foreground mt-1">The Troubleshooting guide under Agents do not start lists the usual causes.</p>
			{/if}
		</div>
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3" disabled={running}>Back</button>{/if}
		{#if !succeeded}
			<button type="button" onclick={start} class="btn {attempted ? 'btn-secondary' : 'btn-primary'} flex-1 py-3" disabled={running}>
				{#if running}<Loader2 size={16} class="inline-block animate-spin mr-2" />Running…{:else}<Play size={16} class="inline-block mr-2" />{attempted ? 'Try again' : 'Run it'}{/if}
			</button>
		{/if}
		{#if succeeded || attempted}
			<button type="button" onclick={onContinue} class="btn {succeeded ? 'btn-primary' : 'btn-secondary'} flex-1 py-3" disabled={running}>
				{succeeded ? 'Continue' : 'Continue anyway'}
			</button>
		{/if}
	</div>
</div>

<style>
	.reply {
		max-height: 16rem;
		overflow-y: auto;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		line-height: 1.55;
	}
</style>
