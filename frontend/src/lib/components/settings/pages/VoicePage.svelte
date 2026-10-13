<script lang="ts">
	/**
	 * Voice: Dictation and Voice calls.
	 *
	 * Dictation's global input, hotkey and sound cues are per-device
	 * preferences (hotkeyPreferences), and so is Voice volume (voiceVolume.ts:
	 * phone speakers and headphones need different levels; a change reaches a
	 * call in progress through the same store). Microphone mode, silence and everything
	 * under Voice calls are worker settings (`GET/PUT /settings/voice`, see
	 * voice-state.svelte.ts). The dictation model itself lives in Default
	 * models; this page links to it.
	 *
	 * Rows render before the settings arrive (controls disabled) so the page
	 * keeps its shape and search can always land on a row.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { Play, Square, LoaderCircle } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { hotkeyPreferences, type HotkeyConfig } from '$lib/stores/hotkeyPreferences.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { getEntry } from '$lib/settings/registry';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import NumberField from '../rows/NumberField.svelte';
	import TextField from '../rows/TextField.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import HotkeyField from '../rows/HotkeyField.svelte';
	import RangeField from '../rows/RangeField.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import CallPromptsEditor from './editors/CallPromptsEditor.svelte';
	import { VoicePreview, VoiceState } from './voice-state.svelte';
	import { voiceVolume, setVoiceVolume, MAX_VOICE_VOLUME } from '$lib/services/voiceVolume';
	import './editors/editors.css';

	type MicMode = 'auto' | 'toggle';
	const MIC_OPTIONS: { value: MicMode; label: string }[] = [
		{ value: 'auto', label: 'Auto' },
		{ value: 'toggle', label: 'Toggle' }
	];

	const voice = new VoiceState();
	const preview = new VoicePreview();
	let promptsOpen = $state(false);

	const s = $derived(voice.settings);
	const ready = $derived(!!s && voice.status === 'ok');
	const locked = $derived(!ready || voice.saving);
	const details = $derived(voice.details);
	const micMode = $derived<MicMode>(s?.voice_input_mode ?? 'auto');
	const callsOn = $derived(!!s?.voice_mode_enabled);
	const unavailable = $derived(voice.status === 'forbidden' ? FORBIDDEN_TEXT : voice.status === 'error' ? 'Unavailable' : 'Loading…');

	const dictationModel = $derived.by(() => {
		if (!s) return unavailable;
		const id = s.voice_input_model ?? '';
		return s.voice_input_models?.find((m) => m.id === id)?.name ?? (id || 'Not chosen');
	});

	const voiceOptions = $derived.by(() => {
		const list = details?.voices ?? [];
		const current = s?.voice_voice ?? '';
		const options = list.map((v) => ({ value: v.id, label: v.label }));
		// A pinned voice the model no longer lists still shows as itself.
		if (current && !list.some((v) => v.id === current)) options.unshift({ value: current, label: current });
		return options;
	});
	const voiceLabel = $derived(voiceOptions.find((o) => o.value === s?.voice_voice)?.label ?? s?.voice_voice ?? '');

	/** The provider a new call runs on: the pin when it is still eligible, else the first by name (server rule). */
	const providers = $derived(voice.providers);
	const effectiveProvider = $derived.by(() => {
		if (!providers?.length) return null;
		const pinned = providers.find((p) => p.id === s?.voice_provider_id);
		if (pinned) return pinned;
		return [...providers].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id))[0];
	});

	const promptsEdited = $derived.by(() => {
		const opts = s?.voice_options ?? {};
		return (details?.prompts ?? []).some((p) => {
			const v = p.key ? opts[p.key] : '';
			return !!v && v !== p.default_text;
		});
	});

	watchResources(['settings'], () => voice.refresh());
	onMount(() => {
		void voice.refresh();
		void voice.loadProviders();
	});
	onDestroy(() => {
		voice.destroy();
		preview.destroy();
	});

	function setPref<K extends 'globalSttEnabled' | 'voiceSoundCues'>(key: K, value: boolean) {
		if (key === 'globalSttEnabled') hotkeyPreferences.setGlobalSttEnabled(value);
		else hotkeyPreferences.setVoiceSoundCues(value);
		flashSaved();
	}

	function setHotkey(config: HotkeyConfig) {
		hotkeyPreferences.setGlobalSttHotkey(config);
		flashSaved();
	}

	function defaultOf<T>(id: string): T {
		return getEntry(id)?.default as T;
	}

	function saveOpening(value: string) {
		if (!s) return;
		void voice.save({ voice_options: { opening: value }, voice_options_model: s.voice_model });
	}
</script>

<PreferencePage category="voice">
	<RowGroup title="Dictation">
		<SettingRow
			id="voice.globalInput"
			value={$hotkeyPreferences.globalSttEnabled}
			onReset={() => setPref('globalSttEnabled', false)}
		>
			<Switch checked={$hotkeyPreferences.globalSttEnabled} onchange={(v) => setPref('globalSttEnabled', v)} />
		</SettingRow>
		{#if $hotkeyPreferences.globalSttEnabled}
			<SettingRow
				id="voice.hotkey"
				value={$hotkeyPreferences.globalSttHotkey}
				onReset={() => setHotkey(defaultOf<HotkeyConfig>('voice.hotkey'))}
			>
				<HotkeyField value={$hotkeyPreferences.globalSttHotkey} onchange={setHotkey} />
			</SettingRow>
		{/if}
		<SettingRow
			id="voice.micMode"
			hint={micMode === 'auto' ? 'Stops after silence' : 'Click the mic to stop'}
			value={s ? micMode : undefined}
			onReset={() => voice.save({ voice_input_mode: 'auto' })}
		>
			<Segmented
				options={MIC_OPTIONS}
				value={micMode}
				disabled={locked}
				onchange={(mode) => voice.save({ voice_input_mode: mode })}
			/>
		</SettingRow>
		{#if micMode === 'auto'}
			<SettingRow
				id="voice.silence"
				value={s?.voice_input_silence_seconds}
				onReset={() => voice.save({ voice_input_silence_seconds: 5 })}
			>
				<NumberField
					value={s?.voice_input_silence_seconds ?? 5}
					min={1}
					max={60}
					unit="s"
					disabled={locked}
					onchange={(n) => voice.save({ voice_input_silence_seconds: Math.round(n) })}
				/>
			</SettingRow>
		{/if}
		<SettingRow
			id="voice.cues"
			value={$hotkeyPreferences.voiceSoundCues}
			onReset={() => setPref('voiceSoundCues', true)}
		>
			<Switch checked={$hotkeyPreferences.voiceSoundCues} onchange={(v) => setPref('voiceSoundCues', v)} />
		</SettingRow>
		<SettingRow id="voice.dictationModel">
			<LinkValue
				label={dictationModel}
				target={{ category: 'models', item: 'dict' }}
				ariaLabel="Dictation model: {dictationModel}. Open in Default models"
			/>
		</SettingRow>
	</RowGroup>

	<RowGroup title="Voice calls">
		<SettingRow
			id="voice.calls"
			value={s ? callsOn : undefined}
			onReset={() => voice.save({ voice_mode_enabled: false })}
		>
			<Switch checked={callsOn} disabled={locked} onchange={(v) => voice.save({ voice_mode_enabled: v })} />
		</SettingRow>
		<SettingRow
			id="voice.voice"
			dim={!callsOn}
			value={s?.voice_voice}
			onReset={() => voice.save({ voice_voice: defaultOf<string>('voice.voice') })}
		>
			{#if s?.voice_voice}
				<button
					type="button"
					class="st-btn ghost icon"
					aria-label={preview.playing === s.voice_voice ? `Stop ${voiceLabel} sample` : `Play ${voiceLabel} sample`}
					aria-pressed={preview.playing === s.voice_voice}
					disabled={!details}
					onclick={() => s && preview.toggle(s.voice_voice)}
				>
					{#if preview.playing === s.voice_voice && preview.loading}<LoaderCircle class="st-spin-icon" />{:else if preview.playing === s.voice_voice}<Square />{:else}<Play />{/if}
				</button>
			{/if}
			{#if s}
			<SelectField
				value={s.voice_voice}
				options={voiceOptions}
				width={180}
				disabled={locked || !details}
				onchange={(id) => {
					preview.stop();
					void voice.save({ voice_voice: id });
				}}
			/>
			{:else}
				<ValueText value={unavailable} />
			{/if}
		</SettingRow>
		<SettingRow
			id="voice.volume"
			hint={$voiceVolume > 100 ? 'Boosted · this device only' : undefined}
			value={$voiceVolume}
			onReset={() => {
				setVoiceVolume(defaultOf<number>('voice.volume'));
				flashSaved();
			}}
		>
			<RangeField
				value={$voiceVolume}
				min={0}
				max={MAX_VOICE_VOLUME}
				step={10}
				format={(v) => `${v}%`}
				valueText={(v) => `${v}%${v > 100 ? ' (boosted)' : ''}`}
				oninput={(v) => {
					setVoiceVolume(v);
					flashSaved();
				}}
			/>
		</SettingRow>
		<SettingRow id="voice.callsRunOn" dim={!callsOn} hint={s?.voice_model} hintMono>
			{#if providers === null}
				<ValueText value={voice.status === 'forbidden' ? FORBIDDEN_TEXT : 'OpenAI API key'} />
			{:else if providers.length > 1 && effectiveProvider}
				<SelectField
					value={effectiveProvider.id}
					options={providers.map((p) => ({ value: p.id, label: p.name }))}
					width={220}
					disabled={locked}
					onchange={(id) => voice.save({ voice_provider_id: id })}
				/>
			{:else if effectiveProvider}
				<LinkValue
					label={effectiveProvider.name}
					target={{ category: 'providers', item: `mp:${effectiveProvider.id}` }}
					ariaLabel="Calls run on {effectiveProvider.name}. Open in Providers"
				/>
			{:else}
				<LinkValue label="Add an OpenAI API key" target={{ category: 'providers' }} />
			{/if}
		</SettingRow>
		<SettingRow
			id="voice.maxCall"
			dim={!callsOn}
			value={s?.voice_max_call_minutes}
			onReset={() => voice.save({ voice_max_call_minutes: defaultOf<number>('voice.maxCall') })}
		>
			<NumberField
				value={s?.voice_max_call_minutes ?? 240}
				min={1}
				unit="min"
				disabled={locked}
				onchange={(n) => voice.save({ voice_max_call_minutes: Math.max(1, Math.round(n)) })}
			/>
		</SettingRow>
		<SettingRow id="voice.opening" dim={!callsOn} value={s?.voice_options?.opening ?? ''} onReset={() => saveOpening('')}>
			<TextField
				value={s?.voice_options?.opening ?? ''}
				placeholder="Wait for me to speak"
				maxlength={2000}
				disabled={locked || !s?.voice_options}
				onchange={saveOpening}
			/>
		</SettingRow>
		<SettingRow id="voice.prompts" dim={!callsOn} expanded={promptsOpen && details && s?.voice_options ? promptsEditor : undefined}>
			{#if details && s?.voice_options}
				<ExpandToggle value={promptsEdited ? 'Edited' : 'Default'} bind:open={promptsOpen} />
			{:else}
				<ValueText value={s ? 'Not available' : unavailable} />
			{/if}
		</SettingRow>
	</RowGroup>

	{#if preview.playing}
		<p class="st-vh" role="status">{preview.loading ? 'Generating sample…' : 'Playing voice sample'}</p>
	{/if}
	{#if preview.error}<p class="st-msg err" role="alert">{preview.error}</p>{/if}
	{#if voice.error}<p class="st-msg err" role="alert">{voice.error}</p>{/if}
</PreferencePage>

{#snippet promptsEditor()}
	{#if details && s?.voice_options}
		{#key s.voice_model}
			<CallPromptsEditor
				options={s.voice_options}
				{details}
				saving={voice.saving}
				onSave={(patch) => voice.save({ voice_options: patch, voice_options_model: s.voice_model })}
				onClose={() => (promptsOpen = false)}
			/>
		{/key}
	{/if}
{/snippet}

<style>
	:global(.settings-shell) :global(.st-spin-icon) {
		animation: st-spin 0.8s linear infinite;
	}
</style>
