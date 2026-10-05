<script lang="ts">
	/**
	 * ClaudeCliPanel — the setup wizard's "Claude Code CLI" status and sign-in panel.
	 *
	 * Shows what the stock CLI reports about its own login (`claude auth
	 * status`): signed in or not, the account, method, plan, CLI version and
	 * binary. Signing in always happens in the CLI, through Anthropic's own
	 * page:
	 *  - Docker and local runs: "Sign in" opens a terminal in the app that runs
	 *    `claude auth login` on the worker. The owner pastes the code the page
	 *    shows into that terminal.
	 *  - The desktop app, the phone app and non-owner devices: the command to
	 *    run in a terminal on the worker, then "Check again".
	 * An Anthropic API key is offered as the alternative. The terminal state
	 * machine is `ClaudeCliLogin`, shared with Settings > Providers > Claude.
	 */
	import { onDestroy, onMount, tick } from 'svelte';
	import ClaudeCliInstall from './providers/setup/ClaudeCliInstall.svelte';
	import { ExternalLink, Loader2, RefreshCw, SquareTerminal } from 'lucide-svelte';
	import { claudePlanLabel, type ClaudeCliStatus } from '$lib/api/claude-cli';
	import { platform } from '$lib/stores/platform.svelte';
	import { ClaudeCliLogin } from './providers/claude-cli-login.svelte';

	interface Props {
		/** Only the install owner may run the sign-in terminal. */
		isAdmin?: boolean;
		/** Called with every fresh status (the wizard gates Continue on it). */
		onStatus?: (status: ClaudeCliStatus) => void;
		/** When set, shows a button for the API-key alternative. */
		onUseApiKey?: () => void;
		/** Kept for callers; the panel always uses the app's button styles. */
		variant?: 'settings' | 'setup';
	}

	let { isAdmin = false, onStatus, onUseApiKey, variant = 'setup' }: Props = $props();

	const btn = $derived(variant === 'setup' ? 'btn btn-secondary' : 'ed-btn');
	const btnPrimary = $derived(variant === 'setup' ? 'btn btn-primary' : 'ed-btn primary');
	const inputClass = $derived(variant === 'setup' ? 'input mono' : 'inp mono');

	let outputEl = $state<HTMLPreElement | null>(null);
	let heading = $state<HTMLHeadingElement | null>(null);
	async function installed() {
		const wasInstalled = cli.status?.installed;
		await cli.load(true);
		if (!wasInstalled && cli.status?.installed) { await tick(); heading?.focus(); }
	}
	const cli = new ClaudeCliLogin({
		onStatus: (s) => onStatus?.(s),
		onOutput: () => {
			if (outputEl) outputEl.scrollTop = outputEl.scrollHeight;
		}
	});

	const status = $derived(cli.status);
	const canUseTerminal = $derived(cli.terminalAvailable(isAdmin, platform.isMobile));
	const planLabel = $derived(claudePlanLabel(status?.plan));

	onMount(() => {
		void cli.init(() => canUseTerminal);
	});

	onDestroy(() => cli.dispose());
</script>

<div class="cli-panel" data-testid="claude-cli-panel">
	{#if !status}
		<p class="hint">{cli.loadError || 'Asking the Claude Code CLI…'}</p>
	{:else if !status.installed}
		<ClaudeCliInstall {isAdmin} onFinished={installed} />
		{#if onUseApiKey}<div class="actions"><button type="button" class={btn} onclick={onUseApiKey}>Use an API key</button></div>{/if}
	{:else}
		<h3 bind:this={heading} tabindex="-1" class="pad">Sign in to Claude Code</h3>
		<dl class="facts">
			<div>
				<dt>Status</dt>
				<dd data-testid="claude-cli-state" class:ok={status.signed_in} class:off={!status.signed_in}>
					{#if !status.installed}
						CLI not installed
					{:else if status.signed_in}
						Signed in
					{:else if status.signed_in === false}
						Not signed in
					{:else}
						Unknown
					{/if}
				</dd>
			</div>
			{#if status.signed_in}
				<div><dt>Account</dt><dd data-testid="claude-cli-email">{status.email ?? 'Not reported by the CLI'}</dd></div>
				{#if status.auth_method_label}<div><dt>Signed in with</dt><dd>{status.auth_method_label}</dd></div>{/if}
				{#if planLabel}<div><dt>Plan</dt><dd>{planLabel}</dd></div>{/if}
				{#if status.org_name}<div><dt>Organization</dt><dd>{status.org_name}</dd></div>{/if}
			{/if}
			{#if status.cli_version}<div><dt>CLI version</dt><dd class="mono">{status.cli_version}</dd></div>{/if}
			{#if status.binary_path}<div><dt>Binary</dt><dd class="mono path">{status.binary_path}</dd></div>{/if}
		</dl>

		{#if status.error}
			<p class="hint pad" role="status">{status.error}</p>
		{/if}
		{#if status.relogin_notice}
			<p class="msg warn pad" role="status" data-testid="claude-cli-relogin">{status.relogin_notice.message}</p>
		{/if}
		{#if status.signed_in}
			<p class="hint pad">Agents in subscription mode run on this account's plan. To switch accounts, sign out (the CLI's <code>{status.login.logout_command}</code>), then sign in again.</p>
		{/if}

		<div class="actions">
			{#if status.installed && !status.signed_in && canUseTerminal && !cli.terminalOpen}
				<button type="button" class={btnPrimary} onclick={() => cli.startLogin()}>
					<SquareTerminal size={14} /> Sign in
				</button>
			{/if}
			{#if status.signed_in && isAdmin}
				<button type="button" class={btn} onclick={() => cli.signOut()} disabled={cli.signingOut}>Sign out</button>
			{/if}
			<button type="button" class={btn} onclick={() => cli.load(true)} disabled={cli.checking}>
				{#if cli.checking}<Loader2 size={14} class="spin" />{:else}<RefreshCw size={14} />{/if} Check again
			</button>
		</div>

		{#if cli.terminalOpen}
			<div class="terminal" aria-label="Claude Code sign-in terminal">
				<p class="hint">Running <code>claude auth login</code> on this worker. Open the link, sign in to your Claude account on Anthropic's page, then paste the code it shows below.</p>
				<pre bind:this={outputEl} class="term-out" data-testid="claude-cli-terminal">{cli.output || 'Starting…'}</pre>
				{#if cli.url}
					<a class="{btn} link" href={cli.url} target="_blank" rel="noopener noreferrer">
						<ExternalLink size={14} /> Open Anthropic's sign-in page
					</a>
				{/if}
				{#if cli.running}
					<div class="code-row">
						<input
							class={inputClass}
							aria-label="Code from Anthropic's page"
							placeholder="Paste the code here"
							autocomplete="off"
							spellcheck="false"
							bind:value={cli.code}
							onkeydown={(e) => { if (e.key === 'Enter') void cli.sendCode(); }}
						/>
						<button type="button" class={btnPrimary} disabled={!cli.code.trim() || cli.sending} onclick={() => cli.sendCode()}>Send</button>
						<button type="button" class={btn} onclick={() => cli.closeTerminal()}>Stop</button>
					</div>
				{:else}
					<div class="code-row">
						{#if cli.loginResult === 'failed'}
							<button type="button" class={btn} onclick={() => cli.startLogin()}>Try again</button>
						{/if}
						<button type="button" class={btn} onclick={() => cli.closeTerminal()}>Close</button>
					</div>
				{/if}
				{#if cli.loginResult === 'ok'}<p class="msg ok" role="status">The CLI reports a successful sign-in.</p>{/if}
				{#if cli.loginError}<p class="msg err" role="alert">{cli.loginError}</p>{/if}
			</div>
		{/if}

		{#if cli.loginError && !cli.terminalOpen}<p class="msg err pad" role="alert">{cli.loginError}</p>{/if}

		{#if !status.signed_in && !cli.terminalOpen}
			<div class="howto" data-testid="claude-cli-instructions">
				{#if status.login.surface === 'docker'}
					<p class="hint">{canUseTerminal ? 'Or sign in from the host:' : 'Sign in from the host:'} open a terminal on the machine running Docker (on Unraid, the >_ Terminal) and run</p>
					<pre class="cmd">{cli.hostCommand}</pre>
					<p class="hint">with your container name for <code>&lt;container&gt;</code>: <code>ai-shuffle</code> with Docker Compose, <code>AI-Shuffle</code> on Unraid. Then choose Check again.</p>
				{:else if !canUseTerminal}
					<p class="hint">Open a terminal on the computer running AI Shuffle and run</p>
					<pre class="cmd">{status.login.command}</pre>
					<p class="hint">Sign in on Anthropic's page, then choose Check again.</p>
				{/if}
			</div>
		{/if}

		<p class="hint pad alt">
			Prefer to pay per use? An Anthropic API key works instead of a subscription and suits scheduled and unattended runs.
			{#if onUseApiKey}
				<button type="button" class="linkish" onclick={onUseApiKey}>Use an API key</button>
			{/if}
		</p>
	{/if}
</div>

<style>
	.cli-panel { display: grid; gap: 8px; padding: 4px 0; }
	.facts { margin: 0; padding: 4px 14px; display: grid; gap: 6px; }
	.facts div { display: flex; justify-content: space-between; gap: 16px; font-size: 13px; }
	.facts dt { color: var(--muted-foreground); }
	.facts dd { margin: 0; text-align: right; min-width: 0; overflow-wrap: anywhere; }
	.facts dd.ok { color: var(--success, #16a34a); }
	.facts dd.off { color: var(--muted-foreground); }
	.mono, code, .cmd, .term-out { font-family: var(--font-mono, ui-monospace, monospace); }
	.path { font-size: 12px; }
	.hint { margin: 0; color: var(--muted-foreground); font-size: 12px; line-height: 1.5; }
	.pad { padding: 0 14px; }
	.actions, .code-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 0 14px; }
	.code-row { padding: 0; }
	.code-row input { flex: 1; min-width: 180px; }
	.terminal { display: grid; gap: 8px; margin: 0 14px; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--muted, transparent); }
	.term-out { margin: 0; max-height: 220px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; line-height: 1.45; padding: 8px; border-radius: 6px; background: var(--background); color: var(--foreground); }
	.cmd { margin: 0; padding: 6px 8px; border-radius: 6px; background: var(--muted, transparent); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; user-select: all; }
	.howto { display: grid; gap: 6px; padding: 0 14px; }
	.link { justify-self: start; text-decoration: none; }
	.msg { margin: 0; font-size: 12.5px; }
	.msg.ok { color: var(--success, #16a34a); }
	.msg.err { color: var(--destructive); }
	.msg.warn { color: var(--warning, #d97706); }
	.linkish { background: none; border: none; padding: 0; color: var(--primary); cursor: pointer; font-size: inherit; text-decoration: underline; }
	.cli-panel button, .cli-panel a.link { display: inline-flex; align-items: center; gap: 6px; }
	.cli-panel .linkish { display: inline; }
	:global(.cli-panel .spin) { animation: cli-spin 1s linear infinite; }
	@keyframes cli-spin { to { transform: rotate(360deg); } }
</style>
