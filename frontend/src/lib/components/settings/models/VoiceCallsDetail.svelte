<script lang="ts">
	/**
	 * VoiceCallsDetail: read-only. New calls always use GPT Live; the voice,
	 * call length and prompts live on the Voice page.
	 */
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import { getModelsState, VOICE_CALL_MODEL } from './tasks.svelte';
	import { VOICE_CALL_ICON } from './icons';

	const { voice } = getModelsState();

	const facts = $derived<Fact[]>([
		{ label: 'Model', value: VOICE_CALL_MODEL, mono: true },
		{ label: 'Runs on', value: voice.settings ? voice.callProviderName : null },
		{ label: 'Voice', value: voice.callVoiceLabel || null },
		{
			label: 'Longest call',
			value: voice.settings?.voice_max_call_minutes ? `${voice.settings.voice_max_call_minutes} min` : null
		}
	]);
</script>

<DetailPane
	name="Voice calls"
	icon={VOICE_CALL_ICON}
	status="ok"
	statusText="Fixed model"
	badge="Audio"
	subtitle="Set by AI Shuffle"
	{facts}
	category="models"
	item="voicecall"
>
	<RowGroup title="Model">
		<SettingRow id="models.voiceCallModel">
			<ValueText value={VOICE_CALL_MODEL} mono tone="fg" />
		</SettingRow>
		<SettingRow label="Runs on">
			<LinkValue
				label={voice.settings ? voice.callProviderName : 'OpenAI API key'}
				target={{ category: 'providers' }}
			/>
		</SettingRow>
		<SettingRow label="Voice and call length">
			<LinkValue label="Voice" target={{ category: 'voice' }} />
		</SettingRow>
	</RowGroup>
</DetailPane>
