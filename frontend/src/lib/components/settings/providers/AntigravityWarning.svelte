<script lang="ts">
	/**
	 * Google's terms warning for a personal Google sign-in, word for word from
	 * the worker (`/antigravity/meta`), with the terms and FAQ one tap away.
	 * Shown before a personal sign-in starts and on a connected personal row.
	 */
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { antigravity } from './antigravity.svelte';

	const meta = $derived(antigravity.meta);
</script>

{#if meta}
	<div data-testid="antigravity-terms-warning">
		<InlineBanner tone="warn">
			{meta.warning}
			<span class="agy-links">
				<a class="pv-link" href={meta.terms_url} target="_blank" rel="noopener noreferrer">Antigravity terms ↗</a>
				<a class="pv-link" href={meta.dashboard_url} target="_blank" rel="noopener noreferrer">Antigravity FAQ ↗</a>
			</span>
		</InlineBanner>
	</div>
{/if}

<style>
	.agy-links { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 6px; }
	.agy-links a { min-height: 32px; display: inline-flex; align-items: center; }
	@media (max-width: 600px), (pointer: coarse) {
		.agy-links a { min-height: 44px; }
	}
</style>
