<script lang="ts">
	/** EmptyDetail: what the detail pane shows with nothing selected, or a quiet state such as "Available after approval". */
	import type { Snippet } from 'svelte';
	import type { SettingsIcon } from '$lib/settings/categories';

	interface Props {
		title?: string;
		message?: string;
		icon?: SettingsIcon;
		children?: Snippet;
	}

	let { title, message = 'Select an item.', icon: Icon, children }: Props = $props();
</script>

<div class="st-empty-detail">
	{#if Icon}<span class="ic"><Icon /></span>{/if}
	{#if title}<h2>{title}</h2>{/if}
	<p>{message}</p>
	{#if children}<div class="acts">{@render children()}</div>{/if}
</div>

<style>
	.st-empty-detail {
		max-width: 700px;
		padding: 56px 32px 96px;
		color: var(--st-mfg);
		font-size: 13px;
		display: grid;
		justify-items: start;
		gap: 6px;
	}
	.ic {
		display: grid;
		place-items: center;
		width: 42px;
		height: 42px;
		border-radius: 10px;
		border: 1px dashed var(--st-border-strong);
		color: var(--st-subtle);
		margin-bottom: 8px;
	}
	.ic :global(svg.lucide) {
		width: 19px;
		height: 19px;
	}
	h2 {
		margin: 0;
		font-size: 15px;
		font-weight: 600;
		color: var(--st-fg);
	}
	p {
		margin: 0;
	}
	.acts {
		margin-top: 10px;
		display: flex;
		gap: 8px;
	}
</style>
