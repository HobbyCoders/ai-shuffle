<script lang="ts">
	/**
	 * Wizard step: the first agent run. One short prompt in a new chat session
	 * (no project, no file tools), with the reply streamed in, so the owner
	 * sees an agent answer before leaving setup.
	 *
	 * The run uses a provider that is ready: the one connected in this wizard
	 * first, else the worker's default, else the first ready one (see
	 * `pickFirstRunModel`). The step names that provider and model, and sends
	 * the model explicitly so a default on an unconnected provider never decides.
	 *
	 * When no ready provider has a model to run (a key saved without picking
	 * one), the step says which provider needs one and links back to the
	 * provider step instead of sending a run the worker would refuse.
	 *
	 * A turn the credential gate refuses (`agent_setup_required`) or the loop
	 * guard holds (`loop_guard_confirm`) shows the worker's own message, with a
	 * way back to the provider step for the first.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { AlertTriangle, Check, Loader2, Play } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import {
		getDefaultModel, getModels, getProviders, hydrateModelRegistry
	} from '$lib/constants/models.svelte';
	import { buildProviders } from '$lib/components/settings/providers/providers-model';
	import { setupReadiness } from './provider-readiness';
	import {
		ensureStarterAgent, FIRST_RUN_PROMPT, LOOP_GUARD_CODE, pickFirstRunModel, SETUP_REQUIRED_CODE,
		startFirstAgentRun, type FirstRunHandle, type FirstRunModel, type FirstRunState
	} from '$lib/services/first-agent-run';

	interface Props {
		/** The provider the owner connected in this wizard, if any. */
		preferredProvider?: string | null;
		onContinue: () => void;
		onBack?: () => void;
		/** Go to the provider step, after the worker refused the run for setup. */
		onFixProvider?: () => void;
	}
	let { preferredProvider = null, onContinue, onBack, onFixProvider }: Props = $props();

	let run = $state<FirstRunState | null>(null);
	let starting = $state(false);
	let startError = $state('');
	let pick = $state<FirstRunModel | null>(null);
	let picking = $state(true);
	/** Ready providers with no model picked, by name, when nothing can run. */
	let needsModel = $state<string[]>([]);
	const BRAND_NAMES: Record<string, string> = { openai: 'OpenAI', grok: 'xAI Grok', openrouter: 'OpenRouter' };
	let handle: FirstRunHandle | null = null;
	let outputEl = $state<HTMLDivElement | null>(null);

	const running = $derived(starting || (!!run && ['starting', 'waiting', 'streaming'].includes(run.phase)));
	const finished = $derived(!!run && ['done', 'failed', 'needs-input'].includes(run.phase));
	const succeeded = $derived(run?.phase === 'done');
	const attempted = $derived(finished || !!startError);
	const refused = $derived(finished && run?.code === SETUP_REQUIRED_CODE);
	const held = $derived(finished && run?.code === LOOP_GUARD_CODE);

	const payer = $derived(pick
		? pick.modelLabel ? `${pick.modelLabel} on ${pick.providerName}` : pick.providerName
		: 'the provider you connected');

	/** Read what is ready now and choose the provider and model for the run. */
	async function choose() {
		picking = true;
		try {
			const [status] = await Promise.all([
				auth.checkAuth().catch(() => auth.current.status),
				providers.refreshAll().catch(() => {}),
				hydrateModelRegistry().catch(() => false),
			]);
			const s = providers.current;
			const views = buildProviders({
				rows: s.providers, claude: s.claude, anthropicKeyMasked: s.anthropicKeyMasked,
				fal: { keySet: s.falKeySet, masked: s.falKeyMasked, enabled: s.falEnabled },
			});
			const readiness = setupReadiness(status?.agent_gate, views, s.providers);
			pick = pickFirstRunModel({
				ready: readiness.ready, preferred: preferredProvider, rows: s.providers, registry: getProviders(),
				defaultModel: getDefaultModel(), claudeModels: getModels(),
			});
			needsModel = pick ? [] : readiness.needsModel.map((id) =>
				BRAND_NAMES[id] ?? s.providers.find((r) => `gw:${r.id}` === id)?.name ?? 'Your gateway');
		} catch {
			pick = null;
		} finally {
			picking = false;
		}
	}

	async function start() {
		if (running) return;
		starting = true; startError = ''; run = null;
		handle?.cancel();
		try {
			const agent = await ensureStarterAgent();
			handle = startFirstAgentRun({
				profileId: agent.id,
				model: pick?.model ?? null,
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

	onMount(() => { void choose(); });
	onDestroy(() => { handle?.cancel(); });
</script>

<div class="space-y-4" data-testid="setup-first-run">
	<p class="text-sm text-muted-foreground">
		Run a one-line task to see an agent answer. It runs in a new chat session with no project and no access to your files.
	</p>
	<div class="bg-muted/30 border border-border rounded-lg px-4 py-3 space-y-2">
		<div>
			<p class="text-xs text-muted-foreground mb-1">The task</p>
			<p class="text-sm text-foreground">{FIRST_RUN_PROMPT}</p>
		</div>
		<div class="border-t border-border pt-2" data-testid="setup-first-run-model">
			<p class="text-xs text-muted-foreground mb-1">Runs on</p>
			<p class="text-sm text-foreground">
				{#if !picking && needsModel.length}<span class="text-muted-foreground">No model picked yet.</span>{:else if picking}<span class="inline-flex items-center gap-2 text-muted-foreground"><Loader2 size={12} class="animate-spin" /> Checking your providers…</span>{:else}{payer}, billed to your own account with that provider.{/if}
			</p>
		</div>
	</div>

	{#if !picking && !pick && needsModel.length}
		<div role="alert" class="bg-warning/10 border border-warning/40 px-4 py-3 rounded-lg text-sm" data-testid="setup-first-run-needs-model">
			<p class="flex items-center gap-2 font-medium text-foreground">
				<AlertTriangle size={16} strokeWidth={2} class="text-warning" />
				{needsModel.join(', ')} {needsModel.length > 1 ? 'need' : 'needs'} a model
			</p>
			<p class="text-muted-foreground mt-1">
				{needsModel.length > 1 ? 'They are' : 'It is'} connected, but no model is picked, so there is nothing to run.
				{#if onFixProvider}<button type="button" class="text-primary underline" onclick={onFixProvider}>Go back to AI provider</button>, open {needsModel[0]} and pick a model.{/if}
			</p>
		</div>
	{/if}

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
			{#if refused && onFixProvider}
				<p class="text-muted-foreground mt-1"><button type="button" class="text-primary underline" onclick={onFixProvider}>Go back to AI provider</button> to connect a provider or finish its sign-in, then try again.</p>
			{:else if !refused && !held}
				<p class="text-muted-foreground mt-1">The Troubleshooting guide under Agents do not start lists the usual causes.</p>
			{/if}
		</div>
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3" disabled={running}>Back</button>{/if}
		{#if !succeeded}
			<button type="button" onclick={start} class="btn {attempted ? 'btn-secondary' : 'btn-primary'} flex-1 py-3" disabled={running || picking || (!pick && needsModel.length > 0)}>
				{#if running}<Loader2 size={16} class="inline-block animate-spin mr-2" />Running…{:else}<Play size={16} class="inline-block mr-2" />{attempted ? 'Try again' : 'Run it'}{/if}
			</button>
		{/if}
		{#if succeeded || attempted || (!pick && needsModel.length > 0)}
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
