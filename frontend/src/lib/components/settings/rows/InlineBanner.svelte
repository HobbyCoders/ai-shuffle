<script lang="ts">
	/**
	 * InlineBanner: a one-line notice inside a page or detail ("Changed
	 * elsewhere, your edits kept [Load latest]"). warn is amber, key is teal
	 * (one-time secrets), info is neutral.
	 */
	import type { Snippet } from 'svelte';
	import { TriangleAlert, KeyRound, Info } from 'lucide-svelte';
	import type { SettingsIcon } from '$lib/settings/categories';

	interface Props {
		tone?: 'warn' | 'key' | 'info';
		icon?: SettingsIcon;
		children: Snippet;
		actions?: Snippet;
	}

	let { tone = 'warn', icon, children, actions }: Props = $props();
	const Icon = $derived(icon ?? (tone === 'key' ? KeyRound : tone === 'info' ? Info : TriangleAlert));
</script>

<div class="st-banner {tone}" role={tone === 'warn' ? 'status' : undefined}>
	<Icon />
	<span class="msg">{@render children()}</span>
	{#if actions}<span class="acts">{@render actions()}</span>{/if}
</div>

<style>
	.st-banner {
		display: flex;
		align-items: center;
		gap: 10px;
		font-size: 13px;
		border-radius: var(--st-radius-md);
		padding: 8px 10px;
		margin: 6px 0 10px;
		border: 1px solid transparent;
	}
	.st-banner.warn {
		background: var(--st-warning-soft);
		border-color: var(--st-warning);
	}
	.st-banner.warn > :global(svg.lucide) {
		color: var(--st-warning);
	}
	.st-banner.key {
		background: var(--st-primary-soft);
	}
	.st-banner.key > :global(svg.lucide) {
		color: var(--st-primary);
	}
	.st-banner.info {
		background: var(--st-muted);
	}
	.st-banner.info > :global(svg.lucide) {
		color: var(--st-mfg);
	}
	.st-banner > :global(svg) { width: 16px; height: 16px; flex: 0 0 16px; align-self: flex-start; margin-top: 2px; }
	.msg { min-width: 0; flex: 1; line-height: 1.5; overflow-wrap: anywhere; }
	@media (max-width: 600px) {
		.st-banner { flex-wrap: wrap; align-items: flex-start; }
		.msg { flex-basis: calc(100% - 28px); }
		.acts { padding-left: 26px; }
	}
	.acts {
		margin-left: auto;
		display: inline-flex;
		gap: 8px;
		align-items: center;
	}
</style>
