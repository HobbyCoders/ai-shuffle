<script lang="ts">
	/**
	 * VoiceSection — Settings → Voice (ruling VM17).
	 *
	 * Runtime-editable knobs backed by `GET/PUT /api/v1/settings/voice`:
	 * the enable flag, model-specific voices, funding provider and call cap.
	 * Runtime details and prompt text come from the server's producing paths.
	 * Refresh on shared settings changes;
	 * the PUT is a partial update, and an explicit `null` provider clears the pin.
	 *
	 * Two traps this section is written around:
	 *
	 * * A settings section under `{#if activeTab === …}` REMOUNTS on every tab
	 *   switch. `loadVoiceSettings` shares the last read until a settings change
	 *   or account boundary invalidates it.
	 * * The one free-form field (max call minutes) is one-way bound, so a fetch
	 *   resolving mid-typing would snap it back. `pendingMinutes` holds the local
	 *   edit and is the only thing rendered while an edit is outstanding; the
	 *   server value is applied on commit (blur / Enter), never underneath the
	 *   caret.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { sessionOwnerEpoch } from '$lib/stores/session-owner';
	import { AudioLines, Mic, Play, Square, LoaderCircle } from 'lucide-svelte';
	import VoiceModelOptions from './VoiceModelOptions.svelte';
	import {
		previewVoice,
		loadVoiceSettings,
		saveVoiceSettings,
		type VoiceSettings,
	} from '$lib/api/voice';

	let settings = $state<VoiceSettings | null>(null);
	let loading = $state(true);
	let saving = $state(false);
	const details = $derived(settings?.model_details?.model === settings?.voice_model ? settings?.model_details : undefined);
	const voices = $derived(details?.voices ?? []);

	let error = $state('');
	let success = $state('');
	const owner = sessionOwnerEpoch();
	let alive = true;
	let request = 0;
	const current = () => alive && owner === sessionOwnerEpoch();

	// Free-text guard: non-null means the user is mid-edit and owns the field.
	let pendingMinutes = $state<string | null>(null);
	let pendingSilence = $state<string | null>(null);
	const minutesValue = $derived(
		pendingMinutes ?? String(settings?.voice_max_call_minutes ?? '')
	);


	async function refreshSettings() {
		const generation = ++request;
		try {
			const value = await loadVoiceSettings();
			if (current() && generation === request && !saving) {
				settings = value;
				error = '';
			}
		} catch (e: any) {
			if (current() && generation === request) error = e?.detail || 'Failed to load voice settings';
		} finally {
			if (current() && generation === request) loading = false;
		}
	}
	watchResources(['settings'], refreshSettings);
	onMount(() => { void refreshSettings(); });

	let previewing = $state<string | null>(null);
	let previewLoading = $state(false);
	let previewError = $state('');
	let previewAbort: AbortController | null = null;
	let audio: HTMLAudioElement | null = null;
	const samples = new Map<string, string>();
	function stopPreview() {
		previewAbort?.abort(); previewAbort = null;
		audio?.pause(); audio = null;
		previewing = null; previewLoading = false;
	}
	async function playPreview(voice: string) {
		const stopping = previewing === voice;
		stopPreview(); previewError = '';
		if (stopping) return;
		const controller = new AbortController(); previewAbort = controller;
		previewing = voice;
		// Create the player in the user gesture, and reuse each downloaded sample.
		const player = new Audio(); audio = player;
		try {
			let url = samples.get(voice);
			if (!url) {
				previewLoading = true;
				const blob = await previewVoice(voice, controller.signal);
				if (!current() || controller.signal.aborted) return;
				url = URL.createObjectURL(blob); samples.set(voice, url);
			}
			if (!current() || controller.signal.aborted) return;
			previewLoading = false;
			player.src = url;
			player.onended = () => { if (audio === player) stopPreview(); };
			player.onerror = () => { if (audio === player) { stopPreview(); previewError = 'Could not play this sample. Try again.'; } };
			await player.play();
		} catch (e: any) {
			if (controller.signal.aborted || !current()) return;
			stopPreview(); previewError = e?.detail || e?.message || 'Voice preview failed. Try again.';
		}
	}

	async function save(patch: Parameters<typeof saveVoiceSettings>[0]) {
		if (!current()) return false;
		++request;
		saving = true;
		error = '';
		success = '';
		try {
			const updated = await saveVoiceSettings(patch);
			if (!current()) return false;
			settings = updated;
			success = 'Saved';
			setTimeout(() => (success = ''), 2000);
			return true;
		} catch (e: any) {
			if (current()) error = e?.detail || 'Failed to save voice settings';
			return false;
		} finally {
			if (current()) {
				saving = false;
				void refreshSettings();
			}
		}
	}

	function commitSilence() {
		const raw = pendingSilence;
		if (raw === null) return;
		const seconds = Number(raw.trim());
		if (!raw.trim() || !Number.isInteger(seconds) || seconds < 1 || seconds > 60) {
			error = 'Silence timeout must be a whole number from 1 to 60 seconds.';
			return;
		}
		pendingSilence = null;
		if (seconds !== (settings?.voice_input_silence_seconds ?? 5)) {
			void save({ voice_input_silence_seconds: seconds });
		}
	}

	function commitMinutes() {
		const raw = pendingMinutes;
		pendingMinutes = null;
		if (raw === null) return;
		const parsed = Number.parseInt(raw.trim(), 10);
		if (!Number.isFinite(parsed) || parsed <= 0) {
			error = 'Max call minutes must be a positive whole number.';
			return;
		}
		if (parsed === settings?.voice_max_call_minutes) return;
		void save({ voice_max_call_minutes: parsed });
	}

	onDestroy(() => {
		alive = false;
		++request;
		stopPreview();
		for (const url of samples.values()) URL.revokeObjectURL(url);
		samples.clear();
	});
</script>

<div class="set-group" data-testid="voice-section">
	<div class="set-grouphead">
		<h3><Mic />Voice transcription</h3>
	</div>

	{#if loading}
		<div class="set-row"><div class="lab"><p>Loading voice settings…</p></div></div>
	{:else if !settings}
		<div class="set-row"><div class="lab"><p class="msg err">{error || 'Voice settings unavailable.'}</p></div></div>
	{:else}
		<section aria-label="Voice transcription">
			<div class="voice-intro">
				<p>Dictate into your chat draft. Voice calls and the global shortcut use separate controls.</p>
			</div>
			<div class="set-row transcription-model-row">
				<div class="lab"><h4>Transcription model</h4><p>Used by the chat microphone and global voice input. Separate from Fal.ai Audio to Text.</p></div>
				<div class="ctl">
					<select class="sel transcription-model" aria-label="Transcription model" value={settings.voice_input_model ?? ''}
						disabled={saving || !settings.voice_input_models?.length}
						onchange={async (e) => {
							const select = e.currentTarget;
							if (!await save({ voice_input_model: select.value })) select.value = settings?.voice_input_model ?? '';
						}}>
						{#if !settings.voice_input_models?.some(model => model.id === settings?.voice_input_model)}
							<option value={settings.voice_input_model ?? ''}>{settings.voice_input_model || 'Model selection unavailable'}</option>
						{/if}
						{#each ['openai', 'fal-ai'] as provider}
							<optgroup label={provider === 'openai' ? 'OpenAI' : 'Fal.ai'}>
								{#each settings.voice_input_models?.filter(model => model.provider === provider) ?? [] as model}
									<option value={model.id}>{model.name}</option>
								{/each}
							</optgroup>
						{/each}
					</select>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Composer microphone mode</h4><p>Auto finishes after silence. Toggle keeps recording until you press the microphone again.</p></div>
				<div class="ctl">
					<select class="sel" aria-label="Composer microphone mode" value={settings.voice_input_mode ?? 'auto'} disabled={saving}
						onchange={(e) => save({ voice_input_mode: e.currentTarget.value as 'auto' | 'toggle' })}>
						<option value="auto">Auto</option>
						<option value="toggle">Toggle</option>
					</select>
				</div>
			</div>
			{#if (settings.voice_input_mode ?? 'auto') === 'auto'}
				<div class="set-row">
					<div class="lab"><h4>Silence timeout (seconds)</h4><p>Wait 1–60 seconds after speech before finishing.</p></div>
					<div class="ctl">
						<input type="number" min="1" max="60" step="1" class="inp minutes" aria-label="Silence timeout (seconds)"
							value={pendingSilence ?? String(settings.voice_input_silence_seconds ?? 5)} disabled={saving}
							oninput={(e) => pendingSilence = e.currentTarget.value} onblur={commitSilence}
							onkeydown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
					</div>
				</div>
			{/if}
		</section>
	{/if}
</div>

{#if settings && !loading}
	<div class="set-group" aria-label="Voice calls">
		<div class="set-grouphead"><h3><AudioLines />Voice calls</h3></div>
		<div class="set-row">
			<div class="lab">
				<h4>Enable voice mode</h4>
			</div>
			<div class="ctl">
				<button
					type="button"
					class="switch"
					class:on={settings.voice_mode_enabled}
					role="switch"
					aria-checked={settings.voice_mode_enabled}
					aria-label="Enable voice mode"
					disabled={saving}
					onclick={() => save({ voice_mode_enabled: !settings?.voice_mode_enabled })}
				><i></i></button>
			</div>
		</div>

		<div class="voice-intro">
			<h4>GPT Live</h4>
			<p>Uses your OpenAI API provider. Tasks use the model selected in your chat composer.</p>
		</div>
		<fieldset class="voice-picker" disabled={saving || !details}>
			<legend>Choose a voice</legend>
			<div class="voice-grid">
				{#each voices as voice}
					<div class="voice-choice" class:selected={settings.voice_voice === voice.id}>
						<label><input type="radio" name="agent-voice" value={voice.id} checked={settings.voice_voice === voice.id} onchange={() => save({voice_voice: voice.id})} /><span>{voice.label}</span></label>
						<button type="button" class="preview-btn" aria-label={previewing === voice.id ? `Stop preview of ${voice.label}` : `Play ${voice.label} voice preview`} aria-pressed={previewing === voice.id} onclick={() => playPreview(voice.id)}>
							{#if previewing === voice.id && previewLoading}<LoaderCircle size={15} class="spin" />{:else if previewing === voice.id}<Square size={14} />{:else}<Play size={15} />{/if}
						</button>
					</div>
				{/each}
			</div>
		</fieldset>
		{#if previewError}<p class="msg err" role="alert">{previewError}</p>{/if}
		{#if previewing}<p class="preview-status" role="status">{previewLoading ? 'Generating sample…' : 'Playing voice preview'}</p>{/if}

		<div class="set-row">
			<div class="lab">
				<h4>Max call minutes</h4>
			</div>
			<div class="ctl">
				<input
					type="number"
					min="1"
					step="1"
					class="inp minutes"
					value={minutesValue}
					disabled={saving}
					oninput={(e) => (pendingMinutes = e.currentTarget.value)}
					onblur={commitMinutes}
					onkeydown={(e) => {
						if (e.key === 'Enter') e.currentTarget.blur();
					}}
					aria-label="Max call minutes"
				/>
			</div>
		</div>


		<p class="call-note">Changes apply to new calls.</p>
		{#if details && settings.voice_options}
			<details class="advanced-voice">
				<summary>Conversation prompts</summary>
				{#key settings.voice_model}
					<VoiceModelOptions options={settings.voice_options} {details} {saving} onSave={patch => save({voice_options:patch, voice_options_model:settings!.voice_model})} />
				{/key}
			</details>
		{/if}

		{#if error}<div class="set-row"><div class="lab"><p class="msg err">{error}</p></div></div>{/if}
		{#if success}<div class="set-row"><div class="lab"><p class="msg ok">{success}</p></div></div>{/if}
	</div>
{/if}

<style>
	.transcription-model { width: 230px; max-width: 100%; }
	@media (max-width: 600px) {
		.transcription-model-row { flex-direction: column; align-items: stretch; }
		.transcription-model-row .ctl, .transcription-model { width: 100%; }
	}
	.voice-intro { padding: 16px 14px; }
	.voice-intro h4 { margin: 0 0 6px; }
	.call-note { padding: 0 14px 14px; margin: 0; color: var(--text-3); font-size: 12px; }
	.advanced-voice { border-top: 1px solid var(--border); padding: 14px; }
	.advanced-voice summary { cursor: pointer; font-size: 13px; font-weight: 500; }
	.voice-intro p { margin: 0; color: var(--text-3); font-size: 13px; line-height: 1.6; }
	.voice-picker { border: 0; padding: 0 14px; margin: 0; min-width: 0; }
	.voice-picker legend { font-size: 13px; font-weight: 500; margin-bottom: 10px; }
	.voice-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: 8px; }
	.voice-choice { display: flex; align-items: center; border: 1px solid var(--border); border-radius: 8px; min-width: 0; }
	.voice-choice.selected { border-color: var(--accent); background: var(--surface-2); }
	.voice-choice label { flex: 1; display: flex; align-items: center; gap: 8px; padding: 12px 8px; cursor: pointer; font-size: 13px; }
	.voice-choice input { accent-color: var(--accent); }
	.preview-btn { display: grid; place-items: center; width: 40px; min-height: 44px; border: 0; border-radius: 6px; background: transparent; color: var(--text-2); cursor: pointer; }
	.preview-btn:hover { color: var(--accent); background: var(--surface-2); }
	.preview-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
	.preview-status { min-height: 18px; color: var(--text-3); font-size: 12px; }
	:global(.preview-btn .spin) { animation: voice-spin 1s linear infinite; }
	@keyframes voice-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { :global(.preview-btn .spin) { animation: none; } }

	.minutes {
		width: 110px;
	}


</style>
