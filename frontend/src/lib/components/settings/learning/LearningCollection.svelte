<script lang="ts">
	/**
	 * Settings > Learned behaviour: what the memory learner taught the agent
	 * about working with you, and how it runs. Setup (on/off, daily calls,
	 * model), recent passes and the built-in guidance sit above the rules.
	 * A rule the learner just wrote carries a New badge and can be undone.
	 */
	import { onMount } from 'svelte';
	import { Activity, BookOpen, GraduationCap, Power } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import RuleDetail from './RuleDetail.svelte';
	import SetupDetail from './SetupDetail.svelte';
	import ActivityDetail from './ActivityDetail.svelte';
	import GuidanceDetail from './GuidanceDetail.svelte';
	import {
		ACTIVITY,
		GUIDANCE,
		LearningModel,
		NEW_RULE,
		SETUP,
		agoText,
		passSummary,
		ruleMeta,
		setupSummary
	} from './learning-model.svelte';

	const CATEGORY = 'learning' as const;
	const model = new LearningModel();
	onMount(() => void model.load());

	let query = $state('');
	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const rules = $derived(model.data?.rules ?? []);
	const shown = $derived.by(() => {
		const q = query.trim().toLowerCase();
		return q ? rules.filter((r) => r.text.toLowerCase().includes(q) || r.applies_label.toLowerCase().includes(q)) : rules;
	});
	const lastPass = $derived(model.data?.activity.find((p) => p.ran));
	const count = $derived(model.data ? (model.newCount ? `${rules.length} · ${model.newCount} new` : rules.length) : undefined);
</script>

<CollectionPage
	category={CATEGORY}
	{count}
	addLabel="Add rule"
	onAdd={() => settingsNav.selectItem(NEW_RULE, CATEGORY)}
	filterPlaceholder="Search rules"
	filterIcon="search"
	listLabel="Learned behaviour"
	bind:filter={query}
>
	{#snippet list()}
		<div class="lb-list" data-setting-anchor="learning.list">
			{#if model.loadError}
				<InlineBanner tone="warn">
					<span role="alert">{model.loadError}</span>
					{#snippet actions()}
						<button type="button" class="st-btn sm" disabled={model.loading} onclick={() => model.load()}>Retry</button>
					{/snippet}
				</InlineBanner>
			{/if}
			<ListItem
				id={SETUP}
				name="Setup"
				icon={Power}
				showIcon
				status={model.data ? (model.data.setup.enabled ? 'ok' : 'off') : undefined}
				summary={model.data ? setupSummary(model.data.setup) : 'Loading…'}
				category={CATEGORY}
			/>
			<ListItem
				id={ACTIVITY}
				name="Activity"
				icon={Activity}
				showIcon
				summary={lastPass ? `${agoText(lastPass.at)}: ${passSummary(lastPass)}` : model.data ? 'No learning passes yet' : 'Loading…'}
				category={CATEGORY}
			/>
			<ListItem
				id={GUIDANCE}
				name="Built-in guidance"
				icon={BookOpen}
				showIcon
				summary="Instructions AI Shuffle ships for each model family"
				category={CATEGORY}
			/>
			<ListGroupLabel label="Rules" compact>
				{#if model.data}<span class="lb-count">{rules.length} / {model.data.limits.learned_rules} learned max</span>{/if}
			</ListGroupLabel>
			{#if model.loading && !model.data}
				<p class="st-empty" role="status">Loading rules…</p>
			{:else if model.data}
				{#each shown as r (r.id)}
					<ListItem id={r.id} name={r.text} category={CATEGORY} summary={ruleMeta(r)}>
						{#snippet badges()}
							{#if r.new}<span class="lb-chip">New</span>{/if}
						{/snippet}
					</ListItem>
				{/each}
				{#if !rules.length}
					<p class="st-empty lb-empty">
						No rules yet. When you correct how an agent works, or it frustrates you, the learner writes a rule here.
					</p>
				{:else if !shown.length}
					<EmptyDetail message="No rules match." actionLabel="Clear" onAction={() => (query = '')} />
				{/if}
			{/if}
		</div>
	{/snippet}
	{#snippet detail()}
		{#if selected === SETUP}
			<SetupDetail {model} />
		{:else if selected === ACTIVITY}
			<ActivityDetail {model} />
		{:else if selected === GUIDANCE}
			<GuidanceDetail {model} />
		{:else if selected === NEW_RULE}
			{#key selected}<RuleDetail {model} id={null} />{/key}
		{:else if selected && model.rule(selected)}
			{#key selected}<RuleDetail {model} id={selected} />{/key}
		{:else if selected && model.data}
			<EmptyDetail icon={GraduationCap} title="Not found" message="This rule was removed, or isn't on this worker." />
		{:else}
			<EmptyDetail
				icon={GraduationCap}
				message="Rules the learner picks up from your corrections appear here. Select one to read, keep, undo or change it."
				idle
			/>
		{/if}
	{/snippet}
</CollectionPage>

<style>
	.lb-list :global(.st-it) {
		align-items: start;
		padding-block: 8px;
	}
	.lb-list :global(.st-it .nm) {
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		white-space: normal;
		overflow: hidden;
		overflow-wrap: anywhere;
		font-weight: 400;
		line-height: 1.4;
	}
	.lb-chip {
		display: inline-flex;
		align-items: center;
		margin-left: 6px;
		padding: 0 7px;
		border-radius: 999px;
		font-size: 11px;
		font-weight: 500;
		line-height: 18px;
		white-space: nowrap;
		color: var(--st-primary);
		background: var(--st-primary-soft);
		box-shadow: inset 0 0 0 1px var(--st-primary-line);
	}
	.lb-count {
		font-size: 12px;
		font-weight: 400;
		color: var(--st-mfg);
		font-variant-numeric: tabular-nums;
	}
	.lb-empty {
		padding: 8px 4px;
	}
</style>
