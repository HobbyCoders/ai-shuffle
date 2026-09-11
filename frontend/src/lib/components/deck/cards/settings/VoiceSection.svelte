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
	import { AudioLines } from 'lucide-svelte';
	import VoiceModelOptions from './VoiceModelOptions.svelte';
	import {
		VOICE_MODELS,
		FAL_VOICE_PROVIDER,
		getVoiceProviders,
		loadVoiceSettings,
		saveVoiceSettings,
		type VoiceProvider,
		type VoiceSettings,
	} from '$lib/api/voice';

	let settings = $state<VoiceSettings | null>(null);
	let providers = $state<VoiceProvider[]>([]);
	let loading = $state(true);
	let saving = $state(false);
	const falSelected = $derived(settings?.voice_provider_id === FAL_VOICE_PROVIDER);
	const models = $derived(VOICE_MODELS.filter(m => (m.id === 'fal-ai/personaplex/realtime') === falSelected));
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
	onMount(async () => {
		await refreshSettings();
		if (!current()) return;
		// A failed provider read is non-fatal: keep the setup pointer visible.
		try {
			const providerList = await getVoiceProviders();
			if (current()) providers = providerList;
		} catch (e) {
			if (current()) console.warn('[voice] could not list eligible providers', e);
		}
	});

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
	});
</script>

<div class="set-group" data-testid="voice-section">
	<div class="set-grouphead">
		<h3><AudioLines />Voice</h3>
	</div>

	{#if loading}
		<div class="set-row"><div class="lab"><p>Loading voice settings…</p></div></div>
	{:else if !settings}
		<div class="set-row"><div class="lab"><p class="msg err">{error || 'Voice settings unavailable.'}</p></div></div>
	{:else}
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

		<div class="set-row">
			<div class="lab">
				<h4>Provider</h4>

			</div>
			<div class="ctl">
				{#if providers.length === 0}
					<p class="msg err">
						Add an OpenAI provider or a fal.ai API key in Settings.
					</p>
				{:else}
					<select
						class="sel"
						value={settings.voice_provider_id ?? ''}
						disabled={saving}
						aria-label="Voice provider"
						onchange={(e) => save({ voice_provider_id: e.currentTarget.value || null })}
					>
						<option value="">Automatic{providers.length === 1 ? ` (${providers[0].name})` : ''}</option>
						{#each providers as provider}
							<option value={provider.id}>{provider.name}</option>
						{/each}
					</select>
				{/if}
			</div>
		</div>

		<div class="set-row">
			<div class="lab">
				<h4>Model</h4>
			</div>
			<div class="ctl">
				<select
					class="sel"
					value={settings.voice_model}
					disabled={saving}
					aria-label="Realtime model"
					onchange={(e) => save({ voice_model: e.currentTarget.value })}
				>
					{#each models as model}
						<option value={model.id}>{model.label}</option>
					{/each}
				</select>
			</div>
		</div>

		<div class="set-row">
			<div class="lab">
				<h4>Voice</h4>
			</div>
			<div class="ctl">
				<select
					class="sel"
					value={settings.voice_voice}
					disabled={saving || !details}
					aria-label="Agent voice"
					onchange={(e) => save({ voice_voice: e.currentTarget.value })}
				>
					{#if !details}<option value={settings.voice_voice}>{settings.voice_voice} (saved)</option>{/if}
					{#each voices as voice}
						<option value={voice.id}>{voice.label}</option>
					{/each}
				</select>

			</div>
		</div>

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


		<div class="set-row">
			<div class="lab">
				<p>{details?.preview_note ?? 'Model details are unavailable. Update the server to load model-specific voices, runtime settings and prompts.'}</p>
				<p>Changes apply to new calls. Existing sessions keep their original model, voice and provider.</p>
				<p>Model availability depends on your provider account. Catalog listings do not guarantee voice-session access.</p>
			</div>
		</div>
		{#if details}
			{#if settings.voice_options}
				{#key settings.voice_model}
					<VoiceModelOptions model={settings.voice_model} providerId={settings.voice_provider_id} options={settings.voice_options} {details} {saving} onSave={patch => save({voice_options:patch, voice_options_model:settings!.voice_model})} />
				{/key}
			{/if}
			<div class="model-details" data-testid="voice-model-details">
				<h4>Model runtime settings</h4>
				<p>Effective saved configuration. Editable values above and system-managed transport values are shown here.</p>
				<dl>
					{#each details.runtime_settings as setting}
						<div><dt>{setting.label}</dt><dd>{setting.value}</dd></div>
					{/each}
				</dl>
				<h4>Voice agent system prompts</h4>
				<p>{details.prompt_note}</p>
				{#each details.prompts as prompt}
					<details class="prompt">
						<summary>{prompt.label}</summary>
						<pre>{prompt.text}</pre>
					</details>
				{/each}
			</div>
		{/if}

		{#if error}<div class="set-row"><div class="lab"><p class="msg err">{error}</p></div></div>{/if}
		{#if success}<div class="set-row"><div class="lab"><p class="msg ok">{success}</p></div></div>{/if}
	{/if}
</div>

<style>
	.minutes {
		width: 110px;
	}
	.set-row .ctl .sel {
		max-width: 260px;
	}

	.model-details { padding: 16px; border-top: 1px solid var(--border); min-width: 0; }
	.model-details h4 { margin: 0 0 8px; font-size: 14px; }
	.model-details p { color: var(--muted-foreground); font-size: 12px; line-height: 1.6; margin: 0 0 12px; }
	dl { margin: 0 0 24px; }
	dl div { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr); gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
	dt { color: var(--muted-foreground); }
	dd { margin: 0; overflow-wrap: anywhere; }
	.prompt { border: 1px solid var(--border); border-radius: 8px; margin-top: 8px; }
	summary { padding: 12px; cursor: pointer; font-size: 13px; }
	pre { margin: 0; padding: 0 12px 12px; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.7 var(--font-mono, monospace); }
	@media (max-width: 480px) {
		dl div { grid-template-columns: minmax(0, 1fr); gap: 4px; }
		.set-row .ctl .sel { max-width: 100%; }
	}
</style>
