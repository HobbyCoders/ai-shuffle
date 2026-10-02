<script lang="ts">
	/**
	 * ToolDetail: one fal.ai media tool. Settings tab: the chosen model's
	 * options (curated rows, or schema-driven parameters for a catalog
	 * model). Model tab: the model list. Danger zone: back to the default.
	 */
	import { onDestroy } from 'svelte';
	import { Play, Square } from 'lucide-svelte';
	import type { ToolDefinition, ToolSetting } from '$lib/types/ai-tool-registry';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import type { StatusTone } from '$lib/settings/types';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import RangeField from '../rows/RangeField.svelte';
	import DynamicModelParams from './DynamicModelParams.svelte';
	import ToolModelList from './ToolModelList.svelte';
	import { OPTION_ENTRY, toolMeta } from './media-tools.svelte';
	import { getModelsState } from './tasks.svelte';
	import { TOOL_ICONS } from './icons';
	import { TtsSample } from './tts-sample.svelte';

	interface Props {
		tool: ToolDefinition;
	}

	let { tool }: Props = $props();

	const { tools } = getModelsState();
	const sample = new TtsSample();
	onDestroy(() => sample.stop());

	const TABS = ['Settings', 'Model'];
	const meta = $derived(toolMeta(tool));
	const modelId = $derived(tools.modelId(tool));
	const curated = $derived(tools.curated(tool));
	const name = $derived(tools.displayName(tool));
	const blocked = $derived(tools.blockedByFal(tool));
	const onFal = $derived(tools.runsOnFal(tool));
	const status = $derived<StatusTone>(!modelId ? 'off' : blocked ? 'warn' : 'ok');
	const statusText = $derived(!modelId ? 'No model chosen' : blocked ? 'fal.ai is off' : 'Ready');
	const price = $derived(tools.price(tool) || (modelId && !curated ? 'Per fal.ai' : null));

	const facts = $derived<Fact[]>([
		{ label: 'Model', value: name || 'None' },
		{ label: 'Model id', value: modelId || null, mono: true },
		{ label: 'Price', value: price },
		{ label: 'Setting', value: tool.modelSettingKey, mono: true }
	]);

	const danger = $derived<DangerAction[]>(
		tool.defaultModel && modelId !== tool.defaultModel
			? [
					{
						label: 'Reset to default model',
						hint: 'Clears this tool’s options',
						button: 'Reset',
						question: 'Reset this tool?',
						onConfirm: () => tools.selectModel(tool, tool.defaultModel).then(() => undefined)
					}
				]
			: []
	);

	function optionLabel(setting: ToolSetting, opt: string): string {
		if (setting.key === 'voice') return tools.voiceLabel(tool, opt);
		if (setting.key === 'duration') return `${opt} s`;
		return setting.type === 'select' ? opt.toUpperCase() : opt;
	}

	function value(setting: ToolSetting): string {
		const v = tools.values[setting.settingKey];
		return v === undefined || v === null || v === 0 ? '' : String(v);
	}

	function chooseModel() {
		settingsNav.setTab('models', meta.item, 'Model');
	}
</script>

{#snippet subtitle()}
	<span class="runs">
		Runs on
		<button
			type="button"
			class="link"
			onclick={() => settingsNav.go({ category: 'providers' })}
		>{onFal ? 'fal.ai media key' : 'OpenAI API key'}</button>
	</span>
{/snippet}

{#snippet banner()}
	{#if blocked}
		<InlineBanner tone="warn">
			fal.ai is off, so this tool cannot run.
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => settingsNav.go({ category: 'providers' })}>
					Open providers
				</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name={meta.name}
	icon={TOOL_ICONS[tool.id]}
	{status}
	{statusText}
	badge={meta.group}
	{subtitle}
	{facts}
	{banner}
	tabs={TABS}
	category="models"
	item={meta.item}
	{danger}
>
	{#snippet children(tab)}
		{#if tab === 'Model'}
			<ToolModelList {tool} />
		{:else if !modelId}
			<RowGroup title="Settings">
				<SettingRow label="No model chosen">
					<button type="button" class="st-btn" onclick={chooseModel}>Choose model</button>
				</SettingRow>
			</RowGroup>
		{:else if !curated}
			{#key modelId}
				<DynamicModelParams endpointId={modelId} persistKey={tool.modelSettingKey} />
			{/key}
		{:else if tool.settings.length === 0}
			<RowGroup title="Settings">
				<SettingRow label="This model has no options" />
			</RowGroup>
		{:else}
			<RowGroup title="Options">
				{#each tool.settings as setting (setting.key)}
					{@const id = OPTION_ENTRY[tool.id]?.[setting.key]}
					{@const options = tools.settingOptions(tool, setting)}
					{#if setting.type === 'slider' && setting.sliderConfig}
						{@const config = setting.sliderConfig}
						<SettingRow {id} label={id ? undefined : setting.label}>
							<RangeField
								value={Number(tools.values[setting.settingKey]) || config.min}
								min={config.min}
								max={config.max}
								step={config.step}
								format={config.format}
								oninput={(v) => tools.setSlider(tool, setting, v)}
							/>
						</SettingRow>
					{:else if options.length > 0}
						<SettingRow {id} label={id ? undefined : setting.label}>
							{#if setting.key === 'voice'}
								{@const voice = value(setting)}
								<button
									type="button"
									class="st-btn icon ghost"
									aria-label={sample.active === voice ? 'Stop sample' : 'Play sample'}
									title={sample.active === voice ? 'Stop sample' : 'Play sample'}
									disabled={!voice || (sample.loading && sample.active === voice)}
									onclick={() =>
										sample.play({
											model: modelId,
											voice,
											speed: Number(tools.values.tts_speed) || undefined
										})}
								>
									{#if sample.active === voice}<Square />{:else}<Play />{/if}
								</button>
							{/if}
							{#if setting.type === 'pills' && options.length <= 4}
								<Segmented
									value={value(setting)}
									options={options.map((o) => ({ value: o, label: optionLabel(setting, o) }))}
									onchange={(v) => tools.setOption(tool, setting, v)}
								/>
							{:else}
								<SelectField
									value={value(setting)}
									options={options.map((o) => ({ value: o, label: optionLabel(setting, o) }))}
									onchange={(v) => tools.setOption(tool, setting, v)}
								/>
							{/if}
						</SettingRow>
					{/if}
				{/each}
			</RowGroup>
		{/if}
	{/snippet}
</DetailPane>

<style>
	.runs {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	.link {
		border: 0;
		background: none;
		padding: 0;
		font-size: inherit;
		color: var(--st-fg);
		text-decoration: underline;
		text-decoration-color: var(--st-border-strong);
		text-underline-offset: 3px;
	}
	.link:hover {
		text-decoration-color: currentColor;
	}
</style>
