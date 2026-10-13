<script lang="ts">
	/**
	 * StudioPageHeader — the Studio-v2 page header (eyebrow + large serif title,
	 * optional accent word, optional right-aligned actions).
	 *
	 * Matches the Build page heading and the mockup's `.phead`, giving the content
	 * surfaces (Files / Notes) the same framing as Sessions and Build. Theme tokens
	 * only — no mockup palette.
	 *
	 * DESKTOP APP. Inside the desktop shell the window's top strip is the page
	 * header: it already names the page in its breadcrumb, so this component
	 * hands its actions to that strip (see $lib/stores/page-header.svelte) and
	 * renders nothing, giving the page the vertical space back. That happens
	 * only while the page is the feature panel on screen; a kept-alive page
	 * off screen neither claims the slot nor draws a header.
	 */
	import { getContext, type Snippet } from 'svelte';
	import { desktop } from '$lib/services/desktop';
	import { pageHeader } from '$lib/stores/page-header.svelte';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from './feature-panel-context';

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

	const panel = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	const token = Symbol('page-header');
	const lifted = $derived(desktop !== null && panel !== undefined && panel.active);

	$effect(() => {
		if (!lifted) return;
		pageHeader.claim(token, actions ?? null);
		return () => pageHeader.release(token);
	});
</script>

{#if !lifted}
	<header class="sph">
		<div class="sph-text">
			{#if eyebrow}<div class="sph-eyebrow">{eyebrow}</div>{/if}
			<h1 class="sph-title">
				{#if title}{title}{/if}{#if title && accent}{' '}{/if}{#if accent}<span class="sph-accent">{accent}</span>{/if}
			</h1>
		</div>
		{#if actions}
			<div class="sph-actions">{@render actions()}</div>
		{/if}
	</header>
{/if}

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
