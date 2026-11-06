<script lang="ts">
	/**
	 * EmptyDetail: what the detail pane shows with nothing selected, or a quiet state such as "Available after approval".
	 *
	 * Inside a list (ListEmptyScope) it is a muted line, because the detail pane
	 * beside it carries the explanation and the create action. On a phone the
	 * list is the whole screen and there is no detail beside it, so a truly
	 * empty list (a create action given, no Clear recovery) shows the full
	 * state instead: icon, title, one line and the create action (the same icon
	 * and label the detail pane's state uses on desktop). A filtered
	 * list keeps its muted line and Clear action everywhere.
	 */
	import { getContext, onMount, type Snippet } from 'svelte';
	import type { SettingsIcon } from '$lib/settings/categories';
	import { platform } from '$lib/stores/platform.svelte';

	interface Props {
		title?: string;
		message?: string;
		icon?: SettingsIcon;
		children?: Snippet;
		/** A single recovery or create action, e.g. actionLabel="Clear". */
		actionLabel?: string;
		onAction?: () => void;
		/** Nothing is selected yet: the page folds this pane away (settings.css). */
		idle?: boolean;
		/** In a list that is the whole page (no detail beside it, e.g. Settings >
		 *  Plugins with nothing added): show the full state on desktop too. */
		full?: boolean;
	}

	let { title, message = 'Select an item.', icon: Icon, children, actionLabel, onAction, idle = false, full = false }: Props = $props();
	const inList = getContext<boolean>('view-panel-empty') ?? false;

	/** Same test as ViewShell's stacked layout: the phone shell, or a phone-width window. */
	let phone = $state(false);
	onMount(() => {
		if (!inList) return;
		const query = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 600px)') : null;
		const update = () => { phone = !!query?.matches; };
		update();
		query?.addEventListener('change', update);
		return () => query?.removeEventListener('change', update);
	});
	/** The list is the whole screen and genuinely empty: show the detail's empty state here. */
	const listHero = $derived(inList && (full || phone || platform.isMobile) && !!children && !actionLabel);
	const quiet = $derived(inList && !listHero);
</script>

<div class="st-empty-detail" class:in-list={quiet} class:list-hero={listHero} data-idle={idle ? '' : undefined}>
	{#if quiet}
		<p>{title ?? message}</p>
	{:else}
		<!-- One icon on desktop and phone alike: 24px, 1.5px stroke, secondary text colour. -->
		{#if Icon}<span class="icon" aria-hidden="true"><Icon size={24} strokeWidth={1.5} /></span>{/if}
		{#if title}<h2>{title}</h2>{/if}
		<p>{message}</p>
	{/if}
	{#if actionLabel || (!quiet && children)}<div class="acts">{#if actionLabel}<button type="button" class="st-btn" onclick={onAction}>{actionLabel}</button>{/if}{#if !quiet}{@render children?.()}{/if}</div>{/if}
</div>

<style>
	.st-empty-detail {
		width: 100%;
		max-width: calc(var(--st-reading-width) + 2 * var(--st-gutter));
		margin-inline: auto;
		padding: 32px var(--st-gutter) 56px;
		min-height: 100%;
		flex: 1;
		color: var(--st-mfg);
		font-size: 13px;
		display: flex;
		flex-direction: column;
		align-items: center;
		text-align: center;
		justify-content: center;
		gap: 6px;
	}
	.in-list { min-height: 0; flex: none; padding-block: 12px; align-items: flex-start; text-align: left; justify-content: flex-start; }
	h2 {
		margin: 0;
		font-size: 15px;
		font-weight: 600;
		color: var(--st-fg);
	}
	p {
		max-width: 42ch;
		line-height: 1.6;
		margin: 0;
	}
	.acts {
		margin-top: 10px;
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	/* The kit's svg.lucide rule draws every Settings icon at 16px; the empty
	   state's icon is the one exception, so it outranks it here. */
	.icon { display: grid; place-items: center; margin-bottom: 6px; color: var(--st-mfg); }
	.icon :global(svg) { width: 24px; height: 24px; stroke-width: 1.5; color: inherit; }
	/* The phone list's full-screen state: centred in the space under the search field. */
	.list-hero { min-height: 0; padding: 24px 16px 64px; font-size: 14px; }
	.list-hero h2 { font-size: 16px; }
	.list-hero .acts { justify-content: center; }
</style>
