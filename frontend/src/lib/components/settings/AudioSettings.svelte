<script lang="ts">
	/**
	 * AudioSettings - TTS model/voice/speed/format config, STT model selection
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';
	import {
		ALL_TTS_MODELS,
		ALL_STT_MODELS,
		PROVIDER_DISPLAY_NAMES,
		type TTSVoice
	} from '$lib/types/ai-models';

	let expanded = $state(false);

	// TTS state
	let currentTtsModel = $state('');
	let selectedTtsModel = $state('');
	let savingTtsModel = $state(false);
	let selectedTtsVoice = $state('alloy');
	let currentTtsVoice = $state('alloy');
	let ttsSpeed = $state(1.0);
	let ttsOutputFormat = $state('mp3');
	let ttsInstructions = $state('');
	let savingTtsSettings = $state(false);

	// STT state
	let currentSttModel = $state('');
	let selectedSttModel = $state('');
	let savingSttModel = $state(false);

	// Feedback
	let audioConfigSuccess = $state('');
	let audioConfigError = $state('');

	onMount(() => {
		loadAudioSettings();
	});

	async function loadAudioSettings() {
		try {
			const settings = await api.get<{
				tts_model: string | null;
				tts_voice: string | null;
				tts_speed: number | null;
				tts_output_format: string | null;
				tts_instructions: string | null;
				stt_model: string | null;
			}>('/settings/integrations');

			currentTtsModel = settings.tts_model || 'gpt-4o-mini-tts';
			selectedTtsModel = currentTtsModel;
			currentSttModel = settings.stt_model || 'whisper-1';
			selectedSttModel = currentSttModel;
			selectedTtsVoice = settings.tts_voice || 'alloy';
			currentTtsVoice = selectedTtsVoice;
			ttsSpeed = settings.tts_speed || 1.0;
			ttsOutputFormat = settings.tts_output_format || 'mp3';
			ttsInstructions = settings.tts_instructions || '';
		} catch (e) {
			console.error('Failed to load audio settings:', e);
		}
	}

	function getAvailableVoices(): TTSVoice[] {
		const model = ALL_TTS_MODELS.find(m => m.id === currentTtsModel);
		return model?.voices || [];
	}

	function isCurrentModelSteerable(): boolean {
		const model = ALL_TTS_MODELS.find(m => m.id === currentTtsModel);
		return model?.capabilities?.steerability ?? false;
	}

	function groupModelsByProvider<T extends { provider: string }>(models: T[]): Record<string, T[]> {
		const groups: Record<string, T[]> = {};
		for (const model of models) {
			const name = PROVIDER_DISPLAY_NAMES[model.provider] || model.provider;
			if (!groups[name]) groups[name] = [];
			groups[name].push(model);
		}
		return groups;
	}

	async function updateTtsModel() {
		if (!selectedTtsModel || selectedTtsModel === currentTtsModel) return;
		savingTtsModel = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/tts', {
				model: selectedTtsModel
			});
			currentTtsModel = selectedTtsModel;
			const model = ALL_TTS_MODELS.find(m => m.id === selectedTtsModel);
			if (model && model.voices.length > 0) {
				selectedTtsVoice = model.voices[0].id;
				currentTtsVoice = model.voices[0].id;
			}
			audioConfigSuccess = 'Updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to update');
		} finally {
			savingTtsModel = false;
		}
	}

	async function updateSttModel() {
		if (!selectedSttModel || selectedSttModel === currentSttModel) return;
		savingSttModel = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/audio/stt', {
				model: selectedSttModel
			});
			currentSttModel = selectedSttModel;
			audioConfigSuccess = 'Updated';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to update');
		} finally {
			savingSttModel = false;
		}
	}

	async function saveTtsSettings() {
		savingTtsSettings = true;
		audioConfigError = '';
		audioConfigSuccess = '';
		try {
			await api.patch<{success: boolean}>('/settings/integrations/tts', {
				model: selectedTtsModel,
				voice: selectedTtsVoice,
				speed: ttsSpeed,
				output_format: ttsOutputFormat,
				instructions: ttsInstructions
			});
			currentTtsModel = selectedTtsModel;
			currentTtsVoice = selectedTtsVoice;
			audioConfigSuccess = 'TTS settings saved';
			setTimeout(() => audioConfigSuccess = '', 3000);
		} catch (e: unknown) {
			audioConfigError = getErrorMessage(e, 'Failed to save');
		} finally {
			savingTtsSettings = false;
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
					{#each Object.entries(groupModelsByProvider(ALL_TTS_MODELS)) as [provider, models] (provider)}
						<optgroup label={provider}>
							{#each models as model (model.id)}
								<option value={model.id}>{model.displayName}</option>
							{/each}
						</optgroup>
					{/each}
				</select>
			</div>

			<!-- TTS Voice -->
			<div class="setting-row">
				<label class="setting-label" for="audio-tts-voice">Voice</label>
				<select id="audio-tts-voice" bind:value={selectedTtsVoice} class="input select">
					{#each getAvailableVoices() as voice (voice.id)}
						<option value={voice.id}>{voice.name} {voice.gender ? `(${voice.gender})` : ''}</option>
					{/each}
					{#if getAvailableVoices().length === 0}
						<option value="alloy">Alloy</option>
						<option value="echo">Echo</option>
						<option value="fable">Fable</option>
						<option value="onyx">Onyx</option>
						<option value="nova">Nova</option>
						<option value="shimmer">Shimmer</option>
					{/if}
				</select>
			</div>

			<!-- TTS Speed -->
			<div class="setting-row">
				<label class="setting-label" for="audio-tts-speed">Speed ({ttsSpeed.toFixed(1)}x)</label>
				<input id="audio-tts-speed" type="range" min="0.25" max="4.0" step="0.1" bind:value={ttsSpeed} class="slider" />
			</div>

			<!-- TTS Format -->
			<div class="setting-row">
				<label class="setting-label" for="audio-tts-format">Output Format</label>
				<select id="audio-tts-format" bind:value={ttsOutputFormat} class="input select compact">
					<option value="mp3">MP3</option>
					<option value="wav">WAV</option>
					<option value="opus">Opus</option>
					<option value="aac">AAC</option>
					<option value="flac">FLAC</option>
				</select>
			</div>

			<!-- Voice Instructions (if steerable) -->
			{#if isCurrentModelSteerable()}
				<div class="setting-row vertical">
					<label class="setting-label" for="audio-tts-instructions">Voice Instructions <span class="badge primary">Steerable</span></label>
					<textarea
						id="audio-tts-instructions"
						bind:value={ttsInstructions}
						placeholder="e.g., Speak in a warm, friendly tone..."
						class="input textarea"
						rows="2"
					></textarea>
				</div>
			{/if}

			<button
				onclick={saveTtsSettings}
				disabled={savingTtsSettings}
				class="btn primary"
			>
				{savingTtsSettings ? 'Saving...' : 'Save TTS Settings'}
			</button>

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
					{#each Object.entries(groupModelsByProvider(ALL_STT_MODELS)) as [provider, models] (provider)}
						<optgroup label={provider}>
							{#each models as model (model.id)}
								<option value={model.id}>{model.displayName}</option>
							{/each}
						</optgroup>
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

	.setting-row.vertical {
		flex-direction: column;
		align-items: stretch;
	}

	.setting-label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
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

	.input::placeholder { color: var(--muted-foreground); }

	.input.compact {
		padding: 6px 10px;
		font-size: 0.8125rem;
	}

	.input.textarea {
		resize: vertical;
		min-height: 60px;
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

	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 8px 16px;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn.primary:hover:not(:disabled) { filter: brightness(1.1); }

	.badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.025em;
	}

	.badge.primary {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
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
