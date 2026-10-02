<script lang="ts">
	/**
	 * TextTasksDetail: the one model + effort behind every internal one-shot
	 * call (session titles, commit messages, PR descriptions, branch names,
	 * agent generation). Warns when the model is Claude and Claude is off.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import ModelPicker from '../ModelPicker.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { getModelsState, TEXT_TASK_USES } from './tasks.svelte';
	import { TEXT_ICON } from './icons';

	const { text, tools } = getModelsState();

	const MODE_LABEL: Record<string, string> = { subscription: 'Subscription', api_key: 'API key', off: 'Off' };

	const runsOn = $derived(
		text.isClaude ? (tools.claudeMode ? (MODE_LABEL[tools.claudeMode] ?? tools.claudeMode) : null) : 'Its credential'
	);
	const facts = $derived<Fact[]>([
		{ label: 'Model', value: text.label, mono: text.label === text.value },
		{ label: 'Provider', value: text.providerName || null },
		{ label: 'Effort', value: text.effortLabel },
		{ label: 'Runs on', value: runsOn }
	]);
</script>

{#snippet banner()}
	{#if text.blocked}
		<InlineBanner tone="warn">
			Claude is off. Pick another model.
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => settingsNav.go({ category: 'providers' })}>
					Open providers
				</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name="Text tasks"
	icon={TEXT_ICON}
	status={text.blocked ? 'warn' : 'ok'}
	statusText={text.blocked ? 'Claude is off' : 'Ready'}
	badge="Text"
	subtitle="Background text jobs"
	facts={text.status === 'ok' ? facts : []}
	{banner}
	category="models"
	item="text"
>
	{#if text.status === 'forbidden'}
		<p class="quiet">{FORBIDDEN_TEXT}</p>
	{:else}
		{#if text.status === 'error'}
			<InlineBanner tone="warn">Could not load the text model.</InlineBanner>
		{/if}
		<RowGroup title="Model">
			<SettingRow id="models.textModel">
				<div class="picker">
					<ModelPicker
						id="text-generation-model"
						value={text.value}
						allowCustom={false}
						onSelect={(value) => text.setModel(value)}
					/>
				</div>
			</SettingRow>
			<SettingRow id="models.textEffort" value={text.effort} onReset={() => text.setEffort('')}>
				<SelectField
					value={text.effort}
					options={text.effortOptions}
					disabled={text.saving}
					onchange={(v) => text.setEffort(v)}
				/>
			</SettingRow>
		</RowGroup>
		<RowGroup title="Used for" bare>
			<ul class="chips">
				{#each TEXT_TASK_USES as use (use)}<li>{use}</li>{/each}
			</ul>
		</RowGroup>
	{/if}
</DetailPane>

<style>
	.picker {
		width: 240px;
		max-width: 100%;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.chips li {
		font-size: 12.5px;
		padding: 3px 10px;
		border-radius: 999px;
		border: 1px solid var(--st-border-strong);
		color: var(--st-mfg);
	}
	.quiet {
		margin: 22px 2px 0;
		color: var(--st-mfg);
		font-size: 13px;
	}
</style>
