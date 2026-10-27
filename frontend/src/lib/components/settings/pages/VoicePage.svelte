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
	import { CODEX_VOICE_MODEL, SUPERGROK_VOICE_MODEL } from '$lib/constants/voice-engines';
	import { NO_CALL_LIMIT, voiceModelLabel } from '$lib/api/voice';
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

	/**
	 * ChatGPT voice (ChatGPT subscription): selecting it is the opt-in. It is
	 * funded only by a connected ChatGPT subscription login (provider_type
	 * "chatgpt"), never an API key. Whether its voice components are ready is
	 * reported for the selected engine only, as a sanitized reason. Its states
	 * are plain helper text, not warnings.
	 */
	const chatgpt = $derived(s?.voice_model === CODEX_VOICE_MODEL);
	const chatgptLabel = voiceModelLabel(CODEX_VOICE_MODEL);
	const chatgptUnavailable = $derived(chatgpt && details?.available === false);
	const chatgptReason = $derived(details?.unavailable_reason || "ChatGPT voice isn't ready yet.");
	/**
	 * Grok voice on a connected SuperGrok login (provider_type "grok"). Selecting
	 * it is the opt-in; it never runs on an xAI API key and has no samples.
	 */
	const supergrok = $derived(s?.voice_model === SUPERGROK_VOICE_MODEL);
	const supergrokLabel = voiceModelLabel(SUPERGROK_VOICE_MODEL);
	/** Engines funded by a subscription login: no voice samples. */
	const loginEngine = $derived(chatgpt || supergrok);
	/**
	 * The server lists the engines it offers now (GPT-Live-1.1 only once the
	 * selected OpenAI key can use it). Older servers send no list.
	 */
	const engineOptions = $derived.by(() => {
		const listed = s?.voice_engines?.length
			? s.voice_engines.map((e) => ({ value: e.id, label: e.label }))
			: [
					{ value: 'gpt-live-1', label: 'GPT Live 1' },
					{ value: 'grok-voice-latest', label: 'Grok Voice' },
					{ value: SUPERGROK_VOICE_MODEL, label: supergrokLabel },
					{ value: CODEX_VOICE_MODEL, label: chatgptLabel }
				];
		const current = s?.voice_model;
		if (current && !listed.some((o) => o.value === current)) listed.push({ value: current, label: voiceModelLabel(current) });
		return listed;
	});
	const engineWidth = $derived(Math.min(340, Math.max(180, 64 + 8 * (engineOptions.find((o) => o.value === s?.voice_model)?.label.length ?? 0))));
	/** Microphone gate (dBFS) for engines that stream server-side audio; absent for others. */
	const micGate = $derived(typeof s?.voice_options?.mic_threshold_db === 'number' ? s.voice_options.mic_threshold_db : null);

	/** The provider a new call runs on: the pin when it is still eligible, else the first by name (server rule). */
	const grok = $derived(s?.voice_model === 'grok-voice-latest');
	const providerType = $derived(chatgpt ? 'chatgpt' : supergrok ? 'grok' : grok ? 'xai' : 'openai');
	const providers = $derived(voice.providers?.filter((p) => (p.provider_type ?? 'openai') === providerType) ?? null);
	const chatgptNeedsLogin = $derived(chatgpt && providers !== null && providers.length === 0);
	const supergrokNeedsLogin = $derived(supergrok && providers !== null && providers.length === 0);
	const engineHint = $derived(
		chatgptNeedsLogin
			? 'Needs a ChatGPT login'
			: chatgptUnavailable
				? 'Not ready yet'
				: supergrokNeedsLogin
					? 'Needs a SuperGrok login'
					: undefined
	);
	/** Only an actionable state earns the line under the engine row. */
	const chatgptHelp = $derived(chatgptNeedsLogin || chatgptUnavailable);
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
			id="voice.engine"
			hint={engineHint}
			value={s?.voice_model}
			onReset={() => voice.save({ voice_model: 'gpt-live-1', voice_provider_id: null })}
			expanded={chatgptHelp ? chatgptNote : supergrokNeedsLogin ? supergrokConnect : undefined}
		>
			<SelectField value={s?.voice_model ?? 'gpt-live-1'} options={engineOptions} width={engineWidth} disabled={locked} onchange={(model) => { preview.stop(); void voice.save({ voice_model: model, voice_provider_id: null }); }} />
		</SettingRow>
		<SettingRow
			label="Voice access"
			hint={loginEngine ? "Doesn't fall back to an API key" : 'Uses an API key'}
		>
			<ValueText value={chatgpt ? 'ChatGPT subscription login · server VAD' : supergrok ? 'SuperGrok login · server VAD' : grok ? 'xAI API key · server VAD' : 'OpenAI API key'} />
		</SettingRow>
		<SettingRow
			id="voice.voice"
			dim={!callsOn}
			hint={loginEngine ? 'No samples for this engine' : undefined}
			value={s?.voice_voice}
			onReset={() => voice.save({ voice_voice: chatgpt ? 'cove' : grok || supergrok ? 'eve' : defaultOf<string>('voice.voice') })}
		>
			{#if s?.voice_voice && loginEngine}
				<!-- The server has no samples for subscription engines; say so instead of failing on click. -->
				<button
					type="button"
					class="st-btn ghost icon"
					aria-label="Samples are not available for {chatgpt ? chatgptLabel : supergrokLabel}"
					title={details?.preview_note || 'Samples are not available for this engine.'}
					disabled
				>
					<Play />
				</button>
			{:else if s?.voice_voice}
				<button
					type="button"
					class="st-btn ghost icon"
					aria-label={preview.playing === s.voice_voice ? `Stop ${voiceLabel} sample` : `Play ${voiceLabel} sample`}
					aria-pressed={preview.playing === s.voice_voice}
					disabled={!details}
					onclick={() => s && preview.toggle(s.voice_voice, s.voice_model)}
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
				<ValueText value={voice.status === 'forbidden' ? FORBIDDEN_TEXT : chatgpt ? 'ChatGPT login' : supergrok ? 'SuperGrok login' : 'OpenAI API key'} />
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
				<LinkValue label={chatgpt ? 'Connect ChatGPT' : supergrok ? 'Connect SuperGrok' : grok ? 'Add an xAI API key' : 'Add an OpenAI API key'} target={{ category: 'providers' }} />
			{/if}
		</SettingRow>
		<SettingRow
			id="voice.maxCall"
			dim={!callsOn}
			value={s?.voice_max_call_minutes}
			hint={s?.voice_max_call_minutes === NO_CALL_LIMIT ? 'No limit: lasts while connected' : undefined}
			onReset={() => voice.save({ voice_max_call_minutes: defaultOf<number>('voice.maxCall') })}
		>
			<NumberField
				value={s?.voice_max_call_minutes ?? defaultOf<number>('voice.maxCall')}
				min={NO_CALL_LIMIT}
				unit="min"
				disabled={locked}
				onchange={(n) => voice.save({ voice_max_call_minutes: Math.max(NO_CALL_LIMIT, Math.round(n)) })}
			/>
		</SettingRow>
		<SettingRow
			id="voice.micGate"
			dim={!callsOn}
			value={micGate ?? undefined}
			onReset={micGate === null ? undefined : () => s && voice.save({ voice_options: { mic_threshold_db: defaultOf<number>('voice.micGate') }, voice_options_model: s.voice_model })}
		>
			{#if micGate !== null}
				<NumberField
					value={micGate}
					min={-80}
					max={-20}
					unit="dBFS"
					disabled={locked}
					onchange={(n) => s && voice.save({ voice_options: { mic_threshold_db: Math.round(n) }, voice_options_model: s.voice_model })}
				/>
			{:else}
				<ValueText value={s ? 'Not available' : unavailable} />
			{/if}
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

{#snippet chatgptNote()}
	<div class="chatgpt-help">
		{#if chatgptNeedsLogin}
			<p>
				Connect your ChatGPT login in Settings → Providers to use this.
				<LinkValue label="Connect ChatGPT" target={{ category: 'providers' }} ariaLabel="Connect a ChatGPT subscription login in Providers" />
			</p>
		{/if}
		{#if chatgptUnavailable}
			<p role="status">{chatgptReason}</p>
		{/if}
	</div>
{/snippet}

{#snippet supergrokConnect()}
	<!-- A plain next step, not a warning: the engine works once a login is connected. -->
	<p class="supergrok-connect">
		<span>Connect your SuperGrok login in Settings → Providers to use this.</span>
		<LinkValue label="Open Providers" target={{ category: 'providers' }} ariaLabel="Open Providers to connect your SuperGrok login" />
	</p>
{/snippet}

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
	/* Plain helper text under the engine row: the same muted voice as row hints. */
	.chatgpt-help p,
	.supergrok-connect {
		margin: 0;
		font-size: 13px;
		line-height: 1.5;
		color: var(--st-mfg);
		text-wrap: pretty;
	}
	.chatgpt-help p + p {
		margin-top: 4px;
	}
	.chatgpt-help :global(.st-vlink),
	.supergrok-connect :global(.st-vlink) {
		padding: 0 2px;
		vertical-align: baseline;
		color: var(--st-fg);
	}
	.chatgpt-help :global(.st-vlink:hover),
	.supergrok-connect :global(.st-vlink:hover) {
		color: var(--st-primary);
	}
</style>
