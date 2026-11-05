<script lang="ts">
	/** EmptyDetail: what the detail pane shows with nothing selected, or a quiet state such as "Available after approval". */
	import type { Snippet } from 'svelte';
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
</script>

<div class="st-empty-detail" data-idle={idle ? '' : undefined}>
	{#if title}<h2>{title}</h2>{/if}
	<p>{message}</p>
	{#if actionLabel || children}<div class="acts">{#if actionLabel}<button type="button" class="st-btn" onclick={onAction}>{actionLabel}</button>{/if}{@render children?.()}</div>{/if}
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
		align-items: flex-start;
		justify-content: center;
		gap: 6px;
	}
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
