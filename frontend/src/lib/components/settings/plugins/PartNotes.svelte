<script lang="ts">
	/**
	 * PartNotes: plugin parts with a sentence each (what was adapted, or why a
	 * part is left out). Notes wrap instead of cutting off, since the sentence
	 * is the point.
	 */
	import type { ComponentNote } from './plugin-parts';

	interface Props {
		notes: ComponentNote[];
	}

	let { notes }: Props = $props();
</script>

<ul class="notes">
	{#each notes as n (n.key)}
		<li>
			<span class="lb">{n.label}</span>
			<span class="nt">{n.note}</span>
		</li>
	{/each}
</ul>

<style>
	.notes {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	li {
		display: grid;
		gap: 2px;
		padding: 9px 16px;
		min-width: 0;
	}
	li + li {
		border-top: 1px solid var(--st-border);
	}
	.lb {
		font-size: 14px;
		font-weight: 450;
		overflow-wrap: anywhere;
	}
	.nt {
		font-size: 12.5px;
		line-height: 1.45;
		color: var(--st-mfg);
		overflow-wrap: anywhere;
	}
	.nt::first-letter {
		text-transform: uppercase;
	}
</style>
