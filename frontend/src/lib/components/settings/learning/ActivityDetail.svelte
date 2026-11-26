<script lang="ts">
	/** Recent learning passes on this worker, newest first, with what each one wrote. */
	import { Activity } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import { agoText, dateText, passSummary, signalText, tokenText, type LearningModel } from './learning-model.svelte';

	interface Props {
		model: LearningModel;
	}

	let { model }: Props = $props();
	const passes = $derived(model.data?.activity ?? []);
	const ran = $derived(passes.filter((p) => p.ran).length);
</script>

<DetailPane
	name="Activity"
	icon={Activity}
	subtitle="Recent learning passes on this worker"
	category="learning"
	item="activity"
	facts={model.data ? [{ label: 'Passes shown', value: String(passes.length) }, { label: 'Model calls', value: String(ran) }] : []}
>
	{#snippet children()}
		<RowGroup title="Passes" count={passes.length || undefined}>
			{#if !passes.length}
				<p class="st-empty lb-pad">{model.data ? 'No learning passes yet. One runs after a chat settles.' : 'Loading…'}</p>
			{:else}
				<ol class="lb-passes">
					{#each passes as p, i (i)}
						<li class="lb-pass" class:fail={p.outcome === 'error' || p.outcome === 'budget'}>
							<div class="lb-row">
								<span class="lb-sum">{passSummary(p)}</span>
								<time class="lb-at" title={dateText(p.at)}>{agoText(p.at)}</time>
							</div>
							{#if p.model || tokenText(p) || p.signals?.length}
								<div class="lb-tok">
									{[p.signals?.length ? `For ${signalText(p.signals)}` : '', p.model, tokenText(p)].filter(Boolean).join(' · ')}
								</div>
							{/if}
							{#if p.rules.length}
								<ul class="lb-rules">
									{#each p.rules as r (r.id)}
										<li>
											{#if r.text && model.rule(r.id)}
												<button type="button" class="lb-link" onclick={() => settingsNav.selectItem(r.id, 'learning')}>{r.text}</button>
											{:else}
												<span class="lb-gone">{r.text ?? 'A rule since removed'}</span>
											{/if}
										</li>
									{/each}
								</ul>
							{/if}
						</li>
					{/each}
				</ol>
			{/if}
		</RowGroup>
	{/snippet}
</DetailPane>

<style>
	.lb-pad {
		padding: 12px 14px;
	}
	.lb-passes {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.lb-pass {
		padding: 10px 14px;
		border-top: 1px solid var(--st-line);
	}
	.lb-pass:first-child {
		border-top: 0;
	}
	.lb-row {
		display: flex;
		align-items: baseline;
		gap: 12px;
	}
	.lb-sum {
		flex: 1;
		min-width: 0;
		font-size: 13px;
		color: var(--st-fg);
	}
	.lb-pass.fail .lb-sum {
		color: var(--st-warn, var(--st-fg));
	}
	.lb-at,
	.lb-tok {
		font-size: 12px;
		color: var(--st-mfg);
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	.lb-tok {
		margin-top: 2px;
	}
	.lb-rules {
		margin: 6px 0 0;
		padding-left: 18px;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.lb-rules li + li {
		margin-top: 3px;
	}
	.lb-link {
		all: unset;
		cursor: pointer;
		color: var(--st-primary);
		overflow-wrap: anywhere;
	}
	.lb-link:hover {
		text-decoration: underline;
	}
	.lb-link:focus-visible {
		outline: 2px solid var(--st-primary);
		outline-offset: 2px;
		border-radius: 2px;
	}
	.lb-gone {
		text-decoration: line-through;
		overflow-wrap: anywhere;
	}
</style>
