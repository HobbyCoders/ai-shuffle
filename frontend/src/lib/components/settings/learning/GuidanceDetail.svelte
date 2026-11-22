<script lang="ts">
	/**
	 * Read-only: the instructions AI Shuffle ships for each model family. They
	 * come with the app; learned rules add to them, never change them.
	 */
	import { BookOpen } from 'lucide-svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import type { LearningModel } from './learning-model.svelte';

	interface Props {
		model: LearningModel;
	}

	let { model }: Props = $props();
	const blocks = $derived(model.data?.guidance ?? []);
</script>

<DetailPane
	name="Built-in guidance"
	icon={BookOpen}
	badge="Read only"
	subtitle="Ships with AI Shuffle. Learned rules add to it and never change it."
	category="learning"
	item="guidance"
	width="reading"
>
	{#snippet children()}
		{#if !blocks.length}
			<p class="st-empty">{model.data ? 'No built-in guidance.' : 'Loading…'}</p>
		{/if}
		{#each blocks as b (b.title)}
			<RowGroup title={b.title}>
				<div class="lb-block">
					<p class="lb-for">For {b.families.join(', ')}</p>
					<p class="lb-body">{b.text}</p>
				</div>
			</RowGroup>
		{/each}
	{/snippet}
</DetailPane>

<style>
	.lb-block {
		padding: 12px 14px;
	}
	.lb-for {
		margin: 0 0 6px;
		font-size: 12px;
		color: var(--st-mfg);
	}
	.lb-body {
		margin: 0;
		font-size: 13px;
		line-height: 1.55;
		color: var(--st-fg);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
</style>
