<script lang="ts">
	/**
	 * "Unofficial" label for a ChatGPT or SuperGrok subscription sign-in, with
	 * its one line of help. `help`: 'tip' adds the "?" HelpTip, 'line' prints
	 * the line after the badge, 'hidden' (inside a button, which cannot hold
	 * another) keeps it as the title and for screen readers.
	 */
	import HelpTip from '../rows/HelpTip.svelte';
	import { UNOFFICIAL_SIGN_IN_HELP } from './providers-model';

	interface Props {
		help?: 'tip' | 'line' | 'hidden';
	}

	let { help = 'hidden' }: Props = $props();
</script>

<span class="unofficial" data-testid="unofficial-badge">
	<span class="pill" title={UNOFFICIAL_SIGN_IN_HELP}
		>Unofficial{#if help === 'hidden'}<span class="sr-only">: {UNOFFICIAL_SIGN_IN_HELP}</span>{/if}</span
	>
	{#if help === 'tip'}<HelpTip text={UNOFFICIAL_SIGN_IN_HELP} about="Unofficial sign-in" />{/if}
	{#if help === 'line'}<span class="line">{UNOFFICIAL_SIGN_IN_HELP}</span>{/if}
</span>

<style>
	.unofficial {
		display: inline-flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 2px;
		vertical-align: middle;
	}
	.pill {
		display: inline-flex;
		align-items: center;
		font-size: 11px;
		line-height: 16px;
		padding: 0 7px;
		border-radius: 999px;
		font-weight: 500;
		white-space: nowrap;
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--warning) 40%, transparent);
	}
	.line {
		margin-left: 6px;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
