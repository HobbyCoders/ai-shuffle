<script lang="ts">
	/**
	 * MemoryHealthList: what needs attention and what the worker repaired,
	 * opened under System > Memory > Memory health. Each reason is a full
	 * sentence, so it wraps under its name instead of sitting in a one-line
	 * value slot.
	 */
	import type { UpdateResultRow } from '../updates-state.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';

	interface Props {
		rows: UpdateResultRow[];
	}

	let { rows }: Props = $props();
</script>

<ul class="st-xlist mh-list">
	{#each rows as row (row.id)}
		<li class="st-xitem mh-item">
			<span class="mh-name"><StatusDot tone={row.tone} />{row.name}</span>
			<span class="mh-text">{row.text}</span>
		</li>
	{/each}
</ul>

<style>
	.mh-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.mh-item {
		display: flex;
		flex-direction: column;
		gap: 3px;
		padding: 10px 14px;
	}
	.mh-name {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		font-size: 13.5px;
		color: var(--st-fg);
	}
	.mh-text {
		font-size: 13px;
		color: var(--st-mfg);
		padding-left: 15px;
		overflow-wrap: anywhere;
	}
</style>
