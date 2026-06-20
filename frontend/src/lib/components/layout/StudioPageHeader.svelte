<script lang="ts">
	/**
	 * StudioPageHeader — the Studio-v2 page header (eyebrow + large serif title,
	 * optional accent word, optional right-aligned actions).
	 *
	 * Matches the Build page heading and the mockup's `.phead`, giving the content
	 * surfaces (Files / Notes) the same framing as Sessions and Build. Theme tokens
	 * only — no mockup palette.
	 */
	import type { Snippet } from 'svelte';

	interface Props {
		/** Small uppercase line above the title. */
		eyebrow?: string;
		/** Main title text. */
		title: string;
		/** Optional highlighted trailing word (rendered in the primary color). */
		accent?: string;
		/** Right-aligned action buttons. */
		actions?: Snippet;
	}

	let { eyebrow, title, accent, actions }: Props = $props();
</script>

<header class="sph">
	<div class="sph-text">
		{#if eyebrow}<div class="sph-eyebrow">{eyebrow}</div>{/if}
		<h1 class="sph-title">
			{#if title}{title} {/if}{#if accent}<span class="sph-accent">{accent}</span>{/if}
		</h1>
	</div>
	{#if actions}
		<div class="sph-actions">{@render actions()}</div>
	{/if}
</header>

<style>
	.sph {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		gap: 16px;
		flex-wrap: wrap;
	}
	.sph-text { min-width: 0; }
	.sph-eyebrow {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.1em;
		color: var(--muted-foreground);
	}
	.sph-title {
		margin: 6px 0 0;
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.6rem;
		font-weight: 700;
		letter-spacing: -0.02em;
		line-height: 1.1;
		color: var(--foreground);
	}
	.sph-accent { color: var(--primary); }
	.sph-actions { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }

	@media (max-width: 639px) {
		.sph-title { font-size: 1.35rem; }
	}
</style>
