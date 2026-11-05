<script lang="ts">
	/** EmptyDetail: what the detail pane shows with nothing selected, or a quiet state such as "Available after approval". */
	import { getContext, type Snippet } from 'svelte';
	import type { SettingsIcon } from '$lib/settings/categories';

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
	}

	let { title, message = 'Select an item.', icon: _icon, children, actionLabel, onAction, idle = false }: Props = $props();
	const inList = getContext<boolean>('view-panel-empty') ?? false;
</script>

<div class="st-empty-detail" class:in-list={inList} data-idle={idle ? '' : undefined}>
	{#if inList}
		<p>{title ?? message}</p>
	{:else}
		{#if title}<h2>{title}</h2>{/if}
		<p>{message}</p>
	{/if}
	{#if actionLabel || (!inList && children)}<div class="acts">{#if actionLabel}<button type="button" class="st-btn" onclick={onAction}>{actionLabel}</button>{/if}{#if !inList}{@render children?.()}{/if}</div>{/if}
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
</style>
