<script lang="ts">
	/**
	 * VoiceSection — Settings → Voice (ruling VM17).
	 *
	 * Runtime-editable knobs backed by `GET/PUT /api/v1/settings/voice`:
	 * the enable flag, the realtime model, the agent's voice (with a preview
	 * of the selected one, VM21), which OpenAI API-key provider row funds
	 * calls, and a cap on total call length. Refresh on shared settings changes;
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
	import { AudioLines, Play, Square } from 'lucide-svelte';
	import {
		VOICE_MODELS,
		voicesForModel,
		FAL_VOICE_PROVIDER,
		getVoiceProviders,
		loadVoiceSettings,
		previewVoice,
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
	const voices = $derived(voicesForModel(settings?.voice_model ?? ''));

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
		if (!current()) return;
		++request;
		saving = true;
		error = '';
		success = '';
		try {
			const updated = await saveVoiceSettings(patch);
			if (!current()) return;
			settings = updated;
			success = 'Saved';
			setTimeout(() => (success = ''), 2000);
		} catch (e: any) {
			if (current()) error = e?.detail || 'Failed to save voice settings';
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

	// ---- Voice preview (VM21) ---------------------------------------------
	// One shared <audio> element for the SELECTED voice; starting a preview
	// stops whatever was playing. Fetched blobs are cached per voice so
	// replays don't re-bill the TTS call.
	let previewing = $state(false);
	let loadingPreview = $state(false);
	let previewAudio: HTMLAudioElement | null = null;
	const previewCache = new Map<string, string>();

	function stopPreview() {
		previewAudio?.pause();
		previewAudio = null;
		previewing = false;
	}

	async function togglePreview() {
		const voiceId = settings?.voice_voice;
		if (!voiceId) return;
		if (previewing) {
			stopPreview();
			return;
		}
		if (loadingPreview) return;
		error = '';
		try {
			let url = previewCache.get(voiceId);
			if (!url) {
				loadingPreview = true;
				const blob = await previewVoice(voiceId);
				url = URL.createObjectURL(blob);
				previewCache.set(voiceId, url);
			}
			const audio = new Audio(url);
			audio.onended = () => {
				if (previewAudio === audio) stopPreview();
			};
			previewAudio = audio;
			previewing = true;
			await audio.play();
		} catch (e: any) {
			stopPreview();
			error = e?.detail || 'Could not play the voice preview.';
		} finally {
			loadingPreview = false;
		}
	}

	function changeVoice(voiceId: string) {
		stopPreview();
		void save({ voice_voice: voiceId });
	}

	onDestroy(() => {
		alive = false;
		++request;
		stopPreview();
		for (const url of previewCache.values()) URL.revokeObjectURL(url);
		previewCache.clear();
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
					disabled={saving}
					aria-label="Agent voice"
					onchange={(e) => changeVoice(e.currentTarget.value)}
				>
					{#each voices as voice}
						<option value={voice.id}>{voice.label}</option>
					{/each}
				</select>
				<button
					type="button"
					class="preview-btn"
					class:playing={previewing}
					disabled={falSelected || saving || (loadingPreview && !previewing)}
					aria-label={previewing ? 'Stop the voice preview' : 'Preview the selected voice'}
					title={previewing ? 'Stop preview' : 'Hear this voice'}
					onclick={togglePreview}
				>
					{#if loadingPreview}
						<span class="preview-spinner" aria-hidden="true"></span>
					{:else if previewing}
						<Square size={12} strokeWidth={2} />
					{:else}
						<Play size={12} strokeWidth={2} />
					{/if}
				</button>
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

	.preview-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		flex-shrink: 0;
		border: 1px solid var(--border);
		border-radius: 50%;
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s ease, border-color 0.15s ease;
	}
	.preview-btn:hover:not(:disabled) {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
	}
	.preview-btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.preview-btn.playing {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
		background: color-mix(in oklch, var(--primary) 10%, transparent);
	}
	.preview-spinner {
		width: 11px;
		height: 11px;
		border: 1.5px solid color-mix(in oklch, var(--primary) 35%, transparent);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: preview-spin 0.8s linear infinite;
	}
	@keyframes preview-spin {
		to {
			transform: rotate(360deg);
		}
	}

	.msg {
		margin: 0;
		font-size: 12.5px;
	}
	.msg.ok {
		color: var(--success, #16a34a);
	}
	.msg.err {
		color: var(--destructive);
	}
</style>
