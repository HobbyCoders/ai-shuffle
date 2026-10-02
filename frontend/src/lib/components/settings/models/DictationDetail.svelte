<script lang="ts">
	/**
	 * DictationDetail: the transcription model behind the chat microphone and
	 * the global voice hotkey (`voice_input_model`). A separate setting from
	 * the fal.ai Speech to text tool, which agents use on files.
	 */
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { DEFAULT_DICTATION_MODEL, getModelsState } from './tasks.svelte';
	import { DICTATION_ICON } from './icons';

	const { voice, tools } = getModelsState();

	const blocked = $derived(tools.falEnabled === false && !!voice.settings && voice.inputOnFal);
	const facts = $derived<Fact[]>([
		{ label: 'Model', value: voice.inputModelName() },
		{ label: 'Provider', value: voice.inputModelProvider() || null },
		{ label: 'Default', value: voice.inputModelName(DEFAULT_DICTATION_MODEL) }
	]);
</script>

{#snippet banner()}
	{#if blocked}
		<InlineBanner tone="warn">
			fal.ai is off. Pick an OpenAI model.
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => settingsNav.go({ category: 'providers' })}>
					Open providers
				</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name="Dictation"
	icon={DICTATION_ICON}
	status={blocked ? 'warn' : 'ok'}
	statusText={blocked ? 'fal.ai is off' : 'Ready'}
	{banner}
	badge="Audio"
	subtitle="Voice into the chat draft"
	facts={voice.status === 'ok' ? facts : []}
	category="models"
	item="dict"
>
	{#if voice.status === 'forbidden'}
		<p class="quiet">{FORBIDDEN_TEXT}</p>
	{:else}
		{#if voice.status === 'error'}
			<InlineBanner tone="warn">Could not load voice settings.</InlineBanner>
		{/if}
		<RowGroup title="Model">
			<SettingRow
				id="models.dictation"
				value={voice.settings ? voice.inputModel : undefined}
				onReset={() => voice.setInputModel(DEFAULT_DICTATION_MODEL)}
			>
				<SelectField
					value={voice.inputModel}
					groups={voice.inputModelGroups}
					disabled={!voice.settings || voice.saving}
					onchange={(v) => voice.setInputModel(v)}
				/>
			</SettingRow>
		</RowGroup>
		<RowGroup title="Related">
			<SettingRow label="Microphone and hotkey">
				<LinkValue label="Voice" target={{ category: 'voice' }} />
			</SettingRow>
			<SettingRow label="Transcribing files">
				<LinkValue label="Speech to text" target={{ category: 'models', item: 'stt' }} />
			</SettingRow>
		</RowGroup>
	{/if}
</DetailPane>

<style>
	.quiet {
		margin: 22px 2px 0;
		color: var(--st-mfg);
		font-size: 13px;
	}
</style>
