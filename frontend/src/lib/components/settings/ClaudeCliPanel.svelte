<script lang="ts">
	/**
	 * ClaudeCliPanel — the "Claude Code CLI" status and sign-in panel.
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
	 * An Anthropic API key is offered as the alternative. Used by Settings →
	 * Providers and by the setup wizard.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { ExternalLink, Loader2, RefreshCw, SquareTerminal } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		cancelClaudeCliLogin,
		claudePlanLabel,
		getClaudeCliStatus,
		logoutClaudeCli,
		readClaudeCliLogin,
		sendClaudeCliLogin,
		startClaudeCliLogin,
		type ClaudeCliStatus
	} from '$lib/api/claude-cli';
	import { platform } from '$lib/stores/platform.svelte';

	interface Props {
		/** Only the install owner may run the sign-in terminal. */
		isAdmin?: boolean;
		/** Called with every fresh status (the wizard gates Continue on it). */
		onStatus?: (status: ClaudeCliStatus) => void;
		/** When set, shows a button for the API-key alternative. */
		onUseApiKey?: () => void;
		/** Settings uses the editor's button styles; the setup wizard the app's. */
		variant?: 'settings' | 'setup';
	}

	let { isAdmin = false, onStatus, onUseApiKey, variant = 'settings' }: Props = $props();

	const btn = $derived(variant === 'setup' ? 'btn btn-secondary' : 'ed-btn');
	const btnPrimary = $derived(variant === 'setup' ? 'btn btn-primary' : 'ed-btn primary');
	const inputClass = $derived(variant === 'setup' ? 'input mono' : 'inp mono');

	let status = $state<ClaudeCliStatus | null>(null);
	let loadError = $state('');
	let checking = $state(false);

	// Login terminal
	let terminalOpen = $state(false);
	let running = $state(false);
	let output = $state('');
	let offset = 0;
	let url = $state<string | null>(null);
	let code = $state('');
	let sending = $state(false);
	let loginError = $state('');
	let loginResult = $state<'ok' | 'failed' | null>(null);
	let pollTimer: ReturnType<typeof setTimeout> | undefined;
	let disposed = false;
	let outputEl = $state<HTMLPreElement | null>(null);

	const canUseTerminal = $derived(!!status?.login.terminal && isAdmin && !platform.isMobile);
	const planLabel = $derived(claudePlanLabel(status?.plan));
	const hostCommand = $derived(status?.login.docker_command ?? status?.login.command ?? 'claude auth login');

	async function load(refresh: boolean) {
		checking = true;
		try {
			const next = await getClaudeCliStatus(refresh);
			if (disposed) return;
			status = next;
			loadError = '';
			onStatus?.(next);
		} catch (e) {
			if (!disposed) loadError = getErrorMessage(e, 'Could not ask the Claude Code CLI for its status.');
		} finally {
			checking = false;
		}
	}

	function schedulePoll(delay = 800) {
		clearTimeout(pollTimer);
		if (!disposed) pollTimer = setTimeout(poll, delay);
	}

	function absorb(state: { output: string; offset: number; reset: boolean; url: string | null; running: boolean; succeeded: boolean | null; error: string | null }) {
		output = state.reset ? state.output : output + state.output;
		offset = state.offset;
		url = state.url ?? url;
		running = state.running;
		if (state.error) loginError = state.error;
		queueMicrotask(() => { if (outputEl) outputEl.scrollTop = outputEl.scrollHeight; });
	}

	async function poll() {
		try {
			const state = await readClaudeCliLogin(offset);
			if (disposed) return;
			absorb(state);
			if (state.running) {
				schedulePoll();
				return;
			}
			loginResult = state.succeeded ? 'ok' : state.active ? 'failed' : null;
			await load(true);
		} catch (e) {
			if (!disposed) {
				loginError = getErrorMessage(e, 'Lost contact with the sign-in terminal.');
				running = false;
			}
		}
	}

	async function startLogin() {
		loginError = '';
		loginResult = null;
		output = '';
		offset = 0;
		url = null;
		terminalOpen = true;
		try {
			absorb(await startClaudeCliLogin());
			schedulePoll(300);
		} catch (e) {
			loginError = getErrorMessage(e, 'Could not start `claude auth login`.');
			running = false;
		}
	}

	async function sendCode() {
		const text = code.trim();
		if (!text || sending) return;
		sending = true;
		try {
			absorb(await sendClaudeCliLogin(text, offset));
			code = '';
			schedulePoll(300);
		} catch (e) {
			loginError = getErrorMessage(e, 'Could not pass the code to the CLI.');
		} finally {
			sending = false;
		}
	}

	let signingOut = $state(false);
	async function signOut() {
		if (signingOut) return;
		signingOut = true;
		loginError = '';
		try {
			const result = await logoutClaudeCli();
			if (!result.success) loginError = result.error || 'The CLI could not sign out.';
		} catch (e) {
			loginError = getErrorMessage(e, 'The CLI could not sign out.');
		} finally {
			signingOut = false;
			await load(true);
		}
	}

	async function closeTerminal() {
		clearTimeout(pollTimer);
		if (running) {
			try {
				await cancelClaudeCliLogin();
			} catch {
				// The server stops an abandoned login on its own.
			}
		}
		running = false;
		terminalOpen = false;
		output = '';
		url = null;
		code = '';
	}

	onMount(() => {
		void load(false).then(async () => {
			// Pick up a login that is still running from before a reload.
			if (!canUseTerminal) return;
			try {
				const state = await readClaudeCliLogin(0);
				if (!disposed && state.active && state.running) {
					terminalOpen = true;
					absorb(state);
					schedulePoll();
				}
			} catch {
				// No terminal to resume.
			}
		});
	});

	onDestroy(() => {
		disposed = true;
		clearTimeout(pollTimer);
	});
</script>

<div class="cli-panel" data-testid="claude-cli-panel">
	{#if !status}
		<p class="hint">{loadError || 'Asking the Claude Code CLI…'}</p>
	{:else}
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
		{#if status.signed_in}
			<p class="hint pad">Agents in subscription mode run on this account's plan. To switch accounts, sign out (the CLI's <code>{status.login.logout_command}</code>), then sign in again.</p>
		{/if}

		<div class="actions">
			{#if status.installed && !status.signed_in && canUseTerminal && !terminalOpen}
				<button type="button" class={btnPrimary} onclick={startLogin}>
					<SquareTerminal size={14} /> Sign in
				</button>
			{/if}
			{#if status.signed_in && isAdmin}
				<button type="button" class={btn} onclick={signOut} disabled={signingOut}>Sign out</button>
			{/if}
			<button type="button" class={btn} onclick={() => load(true)} disabled={checking}>
				{#if checking}<Loader2 size={14} class="spin" />{:else}<RefreshCw size={14} />{/if} Check again
			</button>
		</div>

		{#if terminalOpen}
			<div class="terminal" aria-label="Claude Code sign-in terminal">
				<p class="hint">Running <code>claude auth login</code> on this worker. Open the link, sign in to your Claude account on Anthropic's page, then paste the code it shows below.</p>
				<pre bind:this={outputEl} class="term-out" data-testid="claude-cli-terminal">{output || 'Starting…'}</pre>
				{#if url}
					<a class="{btn} link" href={url} target="_blank" rel="noopener noreferrer">
						<ExternalLink size={14} /> Open Anthropic's sign-in page
					</a>
				{/if}
				{#if running}
					<div class="code-row">
						<input
							class={inputClass}
							aria-label="Code from Anthropic's page"
							placeholder="Paste the code here"
							autocomplete="off"
							spellcheck="false"
							bind:value={code}
							onkeydown={(e) => { if (e.key === 'Enter') void sendCode(); }}
						/>
						<button type="button" class={btnPrimary} disabled={!code.trim() || sending} onclick={sendCode}>Send</button>
						<button type="button" class={btn} onclick={closeTerminal}>Stop</button>
					</div>
				{:else}
					<div class="code-row">
						{#if loginResult === 'failed'}
							<button type="button" class={btn} onclick={startLogin}>Try again</button>
						{/if}
						<button type="button" class={btn} onclick={closeTerminal}>Close</button>
					</div>
				{/if}
				{#if loginResult === 'ok'}<p class="msg ok" role="status">The CLI reports a successful sign-in.</p>{/if}
				{#if loginError}<p class="msg err" role="alert">{loginError}</p>{/if}
			</div>
		{/if}

		{#if loginError && !terminalOpen}<p class="msg err pad" role="alert">{loginError}</p>{/if}

		{#if !status.signed_in && !terminalOpen}
			<div class="howto" data-testid="claude-cli-instructions">
				{#if !status.installed}
					{#if status.login.surface === 'docker'}
						<p class="hint">The container installs the CLI with Anthropic's installer on first start. Check the container log, make sure <code>/home/appuser/.local</code> is mapped, and restart it.</p>
					{:else}
						<p class="hint">Install the CLI with Anthropic's installer, sign in with <code>claude auth login</code>, then restart AI Shuffle.</p>
					{/if}
				{:else if status.login.surface === 'docker'}
					<p class="hint">{canUseTerminal ? 'Or sign in from the host:' : 'Sign in from the host:'} open a terminal on the machine running Docker (on Unraid, the >_ Terminal) and run</p>
					<pre class="cmd">{hostCommand}</pre>
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
	.linkish { background: none; border: none; padding: 0; color: var(--primary); cursor: pointer; font-size: inherit; text-decoration: underline; }
	.cli-panel button, .cli-panel a.link { display: inline-flex; align-items: center; gap: 6px; }
	.cli-panel .linkish { display: inline; }
	:global(.cli-panel .spin) { animation: cli-spin 1s linear infinite; }
	@keyframes cli-spin { to { transform: rotate(360deg); } }
</style>
