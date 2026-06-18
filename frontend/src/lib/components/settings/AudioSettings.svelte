<script lang="ts">
	/**
	 * AudioSettings - TTS model/voice/speed config, STT model selection (fal.ai only)
	 *
	 * All settings auto-save on change (no save button).
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';
	import {
		ALL_TTS_MODELS,
		ALL_STT_MODELS,
		type TTSVoice
	} from '$lib/types/ai-models';
	import { notificationSound } from '$lib/services/notificationSound';

	let expanded = $state(false);

	// Notification sound (alert chime on session complete / question / permission)
	let notifySoundEnabled = $state(true);

	function toggleNotifySound() {
		notifySoundEnabled = !notifySoundEnabled;
		notificationSound.setEnabled(notifySoundEnabled);
		// Preview when turning on so the user hears what it does.
		if (notifySoundEnabled) notificationSound.playComplete();
	}

	// TTS state
	let currentTtsModel = $state('');
	let selectedTtsModel = $state('');
	let savingTtsModel = $state(false);
	let selectedTtsVoice = $state('af_heart');
	let currentTtsVoice = $state('af_heart');
	let ttsSpeed = $state(1.0);
	let ttsOutputFormat = $state('wav');

	// STT state
	let currentSttModel = $state('');
	let selectedSttModel = $state('');
	let savingSttModel = $state(false);

	// Feedback
	let audioConfigSuccess = $state('');
	let audioConfigError = $state('');

	// Debounce timer for speed slider
	let speedDebounceTimer: ReturnType<typeof setTimeout> | null = null;

	onMount(() => {
		loadAudioSettings();
		notifySoundEnabled = notificationSound.isEnabled();
	});

	function showSuccess(msg: string) {
		audioConfigSuccess = msg;
		setTimeout(() => audioConfigSuccess = '', 2000);
	}

	async function loadAudioSettings() {
		try {
			const settings = await api.get<{
				tts_model: string | null;
				tts_voice: string | null;
				tts_speed: number | null;
				tts_output_format: string | null;
				stt_model: string | null;
			}>('/settings/integrations');

			currentTtsModel = settings.tts_model || 'fal-ai/kokoro/american-english';
			selectedTtsModel = currentTtsModel;
			currentSttModel = settings.stt_model || 'fal-ai/whisper';
			selectedSttModel = currentSttModel;
			selectedTtsVoice = settings.tts_voice || 'af_heart';
			currentTtsVoice = selectedTtsVoice;
			ttsSpeed = settings.tts_speed || 1.0;
			ttsOutputFormat = settings.tts_output_format || 'wav';
		} catch (e) {
			console.error('Failed to load audio settings:', e);
		}
	}

	function getAvailableVoices(): TTSVoice[] {
		const model = ALL_TTS_MODELS.find(m => m.id === currentTtsModel);
		return model?.voices || [];
	}

	function getAvailableFormats(): string[] {
		const model = ALL_TTS_MODELS.find(m => m.id === currentTtsModel);
		return model?.outputFormats || ['wav'];
	}

	function formatTtsCost(model: typeof ALL_TTS_MODELS[0]): string {
		const costPer1k = model.pricePerMillion / 1000;
		return `$${costPer1k.toFixed(2)}/1K chars`;
	}

	function formatSttCost(model: typeof ALL_STT_MODELS[0]): string {
		return `$${model.pricePerMinute.toFixed(3)}/min`;
	}

	async function updateTtsModel() {
		if (!selectedTtsModel || selectedTtsModel === currentTtsModel) return;
		savingTtsModel = true;
		audioConfigError = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/tts', {
				model: selectedTtsModel
			});
			currentTtsModel = selectedTtsModel;
			const model = ALL_TTS_MODELS.find(m => m.id === selectedTtsModel);
			if (model && model.voices.length > 0) {
				selectedTtsVoice = model.voices[0].id;
				currentTtsVoice = model.voices[0].id;
				// Auto-save the new default voice
				await api.patch('/settings/integrations/tts', { voice: selectedTtsVoice });
			}
			showSuccess('Updated');
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to update');
		} finally {
			savingTtsModel = false;
		}
	}

	async function updateTtsVoice() {
		if (selectedTtsVoice === currentTtsVoice) return;
		try {
			await api.patch('/settings/integrations/tts', { voice: selectedTtsVoice });
			currentTtsVoice = selectedTtsVoice;
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to save voice');
		}
	}

	function onSpeedChange() {
		if (speedDebounceTimer) clearTimeout(speedDebounceTimer);
		speedDebounceTimer = setTimeout(() => saveTtsSpeed(), 300);
	}

	async function saveTtsSpeed() {
		try {
			await api.patch('/settings/integrations/tts', { speed: ttsSpeed });
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to save speed');
		}
	}

	async function updateTtsFormat() {
		try {
			await api.patch('/settings/integrations/tts', { output_format: ttsOutputFormat });
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to save format');
		}
	}

	async function updateSttModel() {
		if (!selectedSttModel || selectedSttModel === currentSttModel) return;
		savingSttModel = true;
		audioConfigError = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/stt', {
				model: selectedSttModel
			});
			currentSttModel = selectedSttModel;
			showSuccess('Updated');
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to update');
		} finally {
			savingSttModel = false;
		}
	}
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>Audio (TTS/STT)</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<!-- Notification Sound -->
			<div class="setting-row">
				<div class="setting-info">
					<span class="setting-label">Notification Sound</span>
					<span class="setting-desc">Play a chime when a session finishes or needs your input — even when this tab isn't focused</span>
				</div>
				<div class="notify-controls">
					<button
						type="button"
						class="btn-test"
						onclick={() => notificationSound.playComplete()}
						disabled={!notifySoundEnabled}
						aria-label="Test notification sound"
					>
						Test
					</button>
					<button
						type="button"
						onclick={toggleNotifySound}
						class="toggle"
						class:active={notifySoundEnabled}
						role="switch"
						aria-checked={notifySoundEnabled}
						aria-label="Notification Sound"
					>
						<span class="toggle-knob"></span>
					</button>
				</div>
			</div>

			<hr class="divider" />

			<!-- TTS Model -->
			<div class="setting-row">
				<label class="setting-label" for="audio-tts-model">Text-to-Speech Model</label>
				<select
					id="audio-tts-model"
					bind:value={selectedTtsModel}
					onchange={updateTtsModel}
					disabled={savingTtsModel}
					class="input select"
				>
					{#each ALL_TTS_MODELS as model (model.id)}
						<option value={model.id}>{model.displayName} — {formatTtsCost(model)}</option>
					{/each}
				</select>
			</div>

			<!-- TTS Voice -->
			<div class="setting-row">
				<label class="setting-label" for="audio-tts-voice">Voice</label>
				<select
					id="audio-tts-voice"
					bind:value={selectedTtsVoice}
					onchange={updateTtsVoice}
					class="input select"
				>
					{#each getAvailableVoices() as voice (voice.id)}
						<option value={voice.id}>{voice.name} {voice.gender ? `(${voice.gender})` : ''}</option>
					{/each}
				</select>
			</div>

			<!-- TTS Speed -->
			<div class="setting-row">
				<label class="setting-label" for="audio-tts-speed">Speed ({ttsSpeed.toFixed(1)}x)</label>
				<input
					id="audio-tts-speed"
					type="range"
					min="0.5"
					max="2.0"
					step="0.1"
					bind:value={ttsSpeed}
					oninput={onSpeedChange}
					class="slider"
				/>
			</div>

			<!-- TTS Format -->
			<div class="setting-row">
				<label class="setting-label" for="audio-tts-format">Output Format</label>
				<select
					id="audio-tts-format"
					bind:value={ttsOutputFormat}
					onchange={updateTtsFormat}
					class="input select compact"
				>
					{#each getAvailableFormats() as fmt (fmt)}
						<option value={fmt}>{fmt.toUpperCase()}</option>
					{/each}
				</select>
			</div>

			<hr class="divider" />

			<!-- STT Model -->
			<div class="setting-row">
				<label class="setting-label" for="audio-stt-model">Speech-to-Text Model</label>
				<select
					id="audio-stt-model"
					bind:value={selectedSttModel}
					onchange={updateSttModel}
					disabled={savingSttModel}
					class="input select"
				>
					{#each ALL_STT_MODELS as model (model.id)}
						<option value={model.id}>{model.displayName} — {formatSttCost(model)}</option>
					{/each}
				</select>
			</div>

			{#if audioConfigSuccess}<div class="success-msg">{audioConfigSuccess}</div>{/if}
			{#if audioConfigError}<div class="error-msg">{audioConfigError}</div>{/if}
		</div>
	{/if}
</section>

<style>
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}
		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.setting-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 8px 0;
	}

	.setting-label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
	}

	.setting-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.setting-desc {
		font-size: 0.75rem;
		font-weight: 400;
		color: var(--muted-foreground);
		white-space: normal;
	}

	.notify-controls {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-shrink: 0;
	}

	.btn-test {
		padding: 5px 12px;
		background: var(--secondary);
		color: var(--secondary-foreground);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.btn-test:hover:not(:disabled) { background: var(--hover-overlay); }
	.btn-test:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn-test:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
	}

	.toggle:hover { border-color: var(--ring); }
	.toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
		border-radius: 50%;
		transition: transform 200ms ease;
		box-shadow: var(--shadow-s);
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}

	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.input.compact {
		padding: 6px 10px;
		font-size: 0.8125rem;
	}

	.slider {
		width: 100%;
		height: 6px;
		border-radius: calc(var(--radius, 0.75rem) * 0.25);
		background: var(--muted);
		appearance: none;
		cursor: pointer;
	}

	.slider::-webkit-slider-thumb {
		appearance: none;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
		box-shadow: var(--shadow-s);
	}

	.slider::-moz-range-thumb {
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
		border: none;
	}

	.divider {
		border: none;
		border-top: 1px solid var(--border);
		margin: 12px 0;
	}

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}
</style>
