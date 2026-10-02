<script lang="ts">
	/**
	 * RowGroup: a titled card of rows. On a preference page it is an h2
	 * section; inside a DetailPane it becomes an h3 detail section. `tone`
	 * danger is the danger-zone style (use DangerZone for that).
	 */
	import type { Snippet } from 'svelte';
	import { isInDetail } from './row-context';

	interface Props {
		title: string;
		/** Small number after the title ("Seats 2"). */
		count?: string | number;
		/** Right-aligned actions in the title line. */
		actions?: Snippet;
		tone?: 'default' | 'danger';
		/** Render children without the card (custom layouts). */
		bare?: boolean;
		/** DOM id, e.g. for a section-level jump. */
		id?: string;
		children: Snippet;
	}

	let { title, count, actions, tone = 'default', bare = false, id, children }: Props = $props();
	const detail = isInDetail();
	const headingId = $props.id();
</script>

<section class="st-grp" class:detail class:danger={tone === 'danger'} {id} aria-labelledby={headingId}>
	<svelte:element this={detail ? 'h3' : 'h2'} class="st-gt" id={headingId}>
		<span>{title}</span>
		{#if count !== undefined}<span class="n">{count}</span>{/if}
		{#if actions}<span class="act">{@render actions()}</span>{/if}
	</svelte:element>
	{#if bare}
		{@render children()}
	{:else}
		<div class="st-list">{@render children()}</div>
	{/if}
</section>

<style>
	.st-grp {
		margin-bottom: 28px;
	}
	.st-grp.detail {
		margin: 22px 0 0;
	}
	.st-gt {
		display: flex;
		align-items: baseline;
		gap: 8px;
		font-size: 13px;
		font-weight: 500;
		color: var(--st-mfg);
		margin: 0 0 8px 2px;
	}
	.st-grp.detail .st-gt {
		min-height: 26px;
		align-items: center;
	}
	.n {
		font-weight: 400;
		color: var(--st-subtle);
		font-variant-numeric: tabular-nums;
	}
	.act {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-weight: 400;
	}
	.st-list {
		background: var(--st-card);
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius);
		overflow: hidden;
	}
	.st-grp.danger .st-gt {
		color: var(--st-destructive);
	}
	.st-grp.danger .st-list {
		border-color: var(--st-destructive-line);
		background: var(--st-destructive-soft);
	}
	/* Hairlines between rows (SettingRow renders .st-row and .st-rowx siblings). */
	.st-list > :global(.st-row + .st-row),
	.st-list > :global(.st-rowx + .st-row) {
		border-top: 1px solid var(--st-border);
	}
</style>
