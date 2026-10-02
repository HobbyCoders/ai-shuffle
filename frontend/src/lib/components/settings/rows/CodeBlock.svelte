<script lang="ts">
	/**
	 * CodeBlock: a command or snippet the user copies (host sign-in command,
	 * one-time key). `prompt` prefixes a muted "$ ".
	 */
	import { Check, Copy } from 'lucide-svelte';

	interface Props {
		code: string;
		prompt?: boolean;
		copy?: boolean;
		label?: string;
	}

	let { code, prompt = false, copy = true, label = 'Copy' }: Props = $props();
	let copied = $state(false);

	async function doCopy() {
		try {
			await navigator.clipboard.writeText(code);
			copied = true;
			setTimeout(() => (copied = false), 1600);
		} catch {
			copied = false;
		}
	}
</script>

<div class="st-code">
	<pre>{#if prompt}<span class="p">$ </span>{/if}{code}</pre>
	{#if copy}
		<button type="button" class="st-btn icon sm ghost" aria-label={copied ? 'Copied' : label} onclick={doCopy}>
			{#if copied}<Check />{:else}<Copy />{/if}
		</button>
	{/if}
</div>

<style>
	.st-code {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		background: var(--st-code-bg);
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius-md);
		padding: 8px 8px 8px 12px;
	}
	pre {
		flex: 1;
		margin: 2px 0;
		font-family: var(--st-mono);
		font-size: 12px;
		line-height: 1.6;
		white-space: pre-wrap;
		word-break: break-word;
		min-width: 0;
	}
	.p {
		color: var(--st-subtle);
	}
</style>
