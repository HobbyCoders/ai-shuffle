<script lang="ts">
	/**
	 * The in-app `claude auth login` terminal: live output, Anthropic's sign-in
	 * link, and the field that passes the code back to the CLI. The state lives
	 * in a ClaudeCliLogin the parent owns.
	 */
	import { ExternalLink, TriangleAlert } from 'lucide-svelte';
	import type { ClaudeCliLogin } from '../claude-cli-login.svelte';

	interface Props {
		cli: ClaudeCliLogin;
		/** Called when the user closes a finished terminal. */
		onClose?: () => void;
	}

	let { cli, onClose }: Props = $props();
	let outputEl = $state<HTMLPreElement | null>(null);

	$effect(() => {
		void cli.output;
		if (outputEl) outputEl.scrollTop = outputEl.scrollHeight;
	});

	async function close() {
		await cli.closeTerminal();
		onClose?.();
	}
</script>

<div class="pv-card">
	<div class="pv-flow" aria-label="Claude Code sign-in terminal" role="group">
		<div class="pv-flow-head">Running <span class="st-mono">claude auth login</span> on this worker</div>
		<pre bind:this={outputEl} class="pv-term" data-testid="claude-cli-terminal"><span class="p"
				>$ </span>claude auth login
{cli.output || 'Starting…'}</pre>
		{#if cli.url}
			<div>
				<a class="pv-link" href={cli.url} target="_blank" rel="noopener noreferrer">
					Open Anthropic’s sign-in page<ExternalLink />
				</a>
			</div>
		{/if}
		{#if cli.running}
			<div class="code">
				<input
					class="st-mono"
					aria-label="Code from Anthropic's page"
					placeholder="Paste the code here"
					autocomplete="off"
					spellcheck="false"
					bind:value={cli.code}
					onkeydown={(e) => {
						if (e.key === 'Enter') void cli.sendCode();
					}}
				/>
				<button
					type="button"
					class="st-btn primary"
					disabled={!cli.code.trim() || cli.sending}
					onclick={() => cli.sendCode()}>Send</button
				>
				<button type="button" class="st-btn ghost" onclick={close}>Stop</button>
			</div>
		{:else}
			<div class="pv-row-inline">
				{#if cli.loginResult === 'failed'}
					<button type="button" class="st-btn" onclick={() => cli.startLogin()}>Try again</button>
				{/if}
				{#if cli.loginResult !== 'ok'}
					<button type="button" class="st-btn ghost" onclick={close}>Close</button>
				{/if}
			</div>
		{/if}
		{#if cli.loginResult === 'ok'}<p class="pv-msg ok" role="status">The CLI reports a successful sign-in.</p>{/if}
		{#if cli.loginError}<p class="pv-msg err" role="alert"><TriangleAlert />{cli.loginError}</p>{/if}
	</div>
</div>

<style>
	.p {
		color: var(--st-subtle);
	}
	.code {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	.code input {
		flex: 1;
		min-width: 160px;
		height: 32px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		color: var(--st-fg);
		padding: 0 10px;
	}
	.code input:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.pv-msg :global(svg.lucide) {
		width: 14px;
		height: 14px;
		vertical-align: -2px;
		margin-right: 6px;
	}
</style>
