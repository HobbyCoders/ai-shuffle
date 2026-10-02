<script lang="ts">
	/**
	 * ModelOption: one pickable model row (radio, name, id and badges, a
	 * price or note on the right). Used by a tool's Model tab and the catalog.
	 */
	interface Props {
		name: string;
		/** Endpoint id, shown in mono. */
		id: string;
		/** Small text before the id (catalog category). */
		kind?: string;
		badges?: string[];
		/** Right side: price, "Auto-imported", "Default"... */
		meta?: string;
		/** Right side as a badge instead of plain text. */
		metaBadge?: boolean;
		pressed: boolean;
		disabled?: boolean;
		/** Greyed and not pickable (no tool uses the model). */
		unusable?: boolean;
		title?: string;
		onpick?: () => void;
	}

	let {
		name,
		id,
		kind,
		badges = [],
		meta,
		metaBadge = false,
		pressed,
		disabled = false,
		unusable = false,
		title,
		onpick
	}: Props = $props();
</script>

<button
	type="button"
	class="mopt"
	class:unusable
	aria-pressed={pressed}
	aria-disabled={unusable ? 'true' : undefined}
	{disabled}
	{title}
	data-model-id={id}
	onclick={() => {
		if (!unusable && !pressed) onpick?.();
	}}
>
	<span class="radio" aria-hidden="true"></span>
	<span class="body">
		<span class="t">{name}</span>
		<span class="d">
			{#if kind}<span>{kind}</span>{/if}
			<span class="st-mono id">{id}</span>
			{#each badges as badge (badge)}<span class="badge">{badge}</span>{/each}
		</span>
	</span>
	{#if meta}
		{#if metaBadge}<span class="badge">{meta}</span>{:else}<span class="pr">{meta}</span>{/if}
	{:else}<span></span>{/if}
</button>

<style>
	.mopt {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		gap: 12px;
		align-items: center;
		width: 100%;
		min-height: 52px;
		padding: 9px 16px;
		border: 0;
		background: none;
		text-align: left;
	}
	:global(.st-mopts > .mopt + .mopt) {
		border-top: 1px solid var(--st-border);
	}
	.mopt:hover:not(:disabled):not(.unusable) {
		background: var(--st-hover);
	}
	.mopt:focus-visible {
		outline-offset: -2px;
	}
	.radio {
		width: 15px;
		height: 15px;
		border-radius: 50%;
		box-shadow: inset 0 0 0 1.5px var(--st-border-strong);
		transition: box-shadow 0.12s ease;
	}
	.mopt[aria-pressed='true'] .radio {
		box-shadow: inset 0 0 0 4.5px var(--st-primary);
	}
	.body {
		display: grid;
		min-width: 0;
	}
	.t {
		font-size: 13.5px;
		font-weight: 500;
	}
	.d {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px 6px;
		margin-top: 2px;
		font-size: 12px;
		color: var(--st-mfg);
		min-width: 0;
	}
	.id {
		font-size: 11.5px;
		overflow-wrap: anywhere;
	}
	.badge {
		display: inline-flex;
		align-items: center;
		font-size: 11.5px;
		padding: 1px 8px;
		border-radius: 999px;
		background: var(--st-muted);
		color: var(--st-mfg);
		white-space: nowrap;
		font-weight: 500;
	}
	.pr {
		font-size: 12.5px;
		color: var(--st-mfg);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.mopt:disabled,
	.mopt.unusable {
		cursor: not-allowed;
	}
	.mopt:disabled .body,
	.mopt.unusable .body,
	.mopt.unusable .radio {
		opacity: 0.5;
	}
	@media (prefers-reduced-motion: reduce) {
		.radio {
			transition: none;
		}
	}
</style>
