<script lang="ts">
	/**
	 * VoiceSettings — Phase 3 realtime voice configuration.
	 *
	 * Lets admins pick STT/TTS providers, voice, turn detector, interruptions,
	 * max duration, and agent-SDK routing. Pricing is shown inline so users
	 * understand the cost impact of each choice.
	 */
	import { onMount } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';

	interface VoiceConfig {
		stt_provider: string;
		tts_provider: string;
		tts_voice_id: string;
		turn_detector: string;
		llm_model: string;
		interruptions_enabled: boolean;
		max_duration_seconds: number;
		use_agent_sdk: boolean;
		deepgram_available: boolean;
		groq_available: boolean;
		cartesia_available: boolean;
		elevenlabs_available: boolean;
	}

	interface CatalogEntry {
		id: string;
		name: string;
		language?: string;
		description?: string;
	}

	interface PricingEntry {
		provider: string;
		model: string;
		unit: string;
		price_usd: number;
	}

	let config = $state<VoiceConfig | null>(null);
	let cartesiaVoices = $state<CatalogEntry[]>([]);
	let elevenlabsVoices = $state<CatalogEntry[]>([]);
	let pricing = $state<{ stt: PricingEntry[]; tts: PricingEntry[] } | null>(null);
	let loading = $state(true);
	let saving = $state(false);
	let errorMsg = $state('');
	let successMsg = $state('');

	onMount(async () => {
		await loadAll();
	});

	async function loadAll() {
		loading = true;
		try {
			config = await api.get<VoiceConfig>('/voice/config');
			pricing = await api.get<{ stt: PricingEntry[]; tts: PricingEntry[] }>('/voice/pricing');
			// Load catalogs only for providers with keys present — avoid 400s.
			if (config.cartesia_available) {
				try {
					const r = await api.get<{ voices: CatalogEntry[] }>('/voice/catalogs/cartesia');
					cartesiaVoices = r.voices ?? [];
				} catch {
					cartesiaVoices = [];
				}
			}
			if (config.elevenlabs_available) {
				try {
					const r = await api.get<{ voices: CatalogEntry[] }>('/voice/catalogs/elevenlabs');
					elevenlabsVoices = r.voices ?? [];
				} catch {
					elevenlabsVoices = [];
				}
			}
		} catch (e) {
			errorMsg = getErrorMessage(e, 'Failed to load voice config');
		} finally {
			loading = false;
		}
	}

	async function save() {
		if (!config) return;
		saving = true;
		errorMsg = '';
		successMsg = '';
		try {
			await api.patch('/voice/config', {
				stt_provider: config.stt_provider,
				tts_provider: config.tts_provider,
				tts_voice_id: config.tts_voice_id,
				turn_detector: config.turn_detector,
				llm_model: config.llm_model,
				interruptions_enabled: config.interruptions_enabled,
				max_duration_seconds: config.max_duration_seconds,
				use_agent_sdk: config.use_agent_sdk
			});
			successMsg = 'Saved ✓';
			setTimeout(() => (successMsg = ''), 2500);
		} catch (e) {
			errorMsg = getErrorMessage(e, 'Failed to save');
		} finally {
			saving = false;
		}
	}

	let currentVoiceList = $derived(
		config?.tts_provider === 'elevenlabs' ? elevenlabsVoices : cartesiaVoices
	);

	let sttPricing = $derived.by(() => {
		if (!config || !pricing) return null;
		return pricing.stt.find((p) => p.provider === config.stt_provider) ?? null;
	});

	let ttsPricing = $derived.by(() => {
		if (!config || !pricing) return null;
		return pricing.tts.find((p) => p.provider === config.tts_provider) ?? null;
	});
</script>

<section class="voice-settings">
	<header>
		<h3>Realtime Voice</h3>
		<p class="sub">
			Configure the Pipecat pipeline — STT, TTS, turn detection, and budget caps.
		</p>
	</header>

	{#if loading}
		<p class="muted">Loading…</p>
	{:else if !config}
		<p class="muted">Voice config unavailable.</p>
	{:else}
		<div class="grid">
			<!-- STT -->
			<label class="field">
				<span class="label">Speech-to-Text</span>
				<select bind:value={config.stt_provider}>
					<option value="deepgram" disabled={!config.deepgram_available}>
						Deepgram Nova-2 (streaming)
						{#if !config.deepgram_available}— key missing{/if}
					</option>
					<option value="groq" disabled={!config.groq_available}>
						Groq Whisper-v3-turbo (batch)
						{#if !config.groq_available}— key missing{/if}
					</option>
				</select>
				{#if sttPricing}
					<span class="pricing">~${sttPricing.price_usd.toFixed(4)}/min</span>
				{/if}
			</label>

			<!-- TTS -->
			<label class="field">
				<span class="label">Text-to-Speech</span>
				<select bind:value={config.tts_provider}>
					<option value="cartesia" disabled={!config.cartesia_available}>
						Cartesia Sonic (streaming)
						{#if !config.cartesia_available}— key missing{/if}
					</option>
					<option value="elevenlabs" disabled={!config.elevenlabs_available}>
						ElevenLabs Turbo v2.5 (streaming)
						{#if !config.elevenlabs_available}— key missing{/if}
					</option>
				</select>
				{#if ttsPricing}
					<span class="pricing">~${ttsPricing.price_usd.toFixed(3)}/1K chars</span>
				{/if}
			</label>

			<!-- Voice -->
			<label class="field">
				<span class="label">Voice</span>
				{#if currentVoiceList.length === 0}
					<input
						type="text"
						bind:value={config.tts_voice_id}
						placeholder="Voice ID"
					/>
					<span class="hint">Paste a voice ID — catalog unavailable (no key?)</span>
				{:else}
					<select bind:value={config.tts_voice_id}>
						{#each currentVoiceList as v}
							<option value={v.id}>
								{v.name}{v.language ? ` · ${v.language}` : ''}
							</option>
						{/each}
					</select>
				{/if}
			</label>

			<!-- Turn detection -->
			<label class="field">
				<span class="label">Turn detection</span>
				<select bind:value={config.turn_detector}>
					<option value="vad">VAD only (fastest)</option>
					<option value="semantic">Semantic (SmartTurn — fewer false interruptions)</option>
				</select>
			</label>

			<!-- LLM model -->
			<label class="field">
				<span class="label">LLM model</span>
				<input type="text" bind:value={config.llm_model} placeholder="claude-sonnet-4-5" />
			</label>

			<!-- Max duration -->
			<label class="field">
				<span class="label">Max call duration (seconds)</span>
				<input
					type="number"
					bind:value={config.max_duration_seconds}
					min="30"
					max="3600"
					step="30"
				/>
			</label>
		</div>

		<div class="toggles">
			<label class="toggle">
				<input type="checkbox" bind:checked={config.interruptions_enabled} />
				<span>Allow user to interrupt the bot</span>
			</label>
			<label class="toggle">
				<input type="checkbox" bind:checked={config.use_agent_sdk} />
				<span>
					Route through Claude Agent SDK
					<small>(enables tools / MCP / worktree — higher latency)</small>
				</span>
			</label>
		</div>

		<div class="actions">
			<button class="btn primary" onclick={save} disabled={saving}>
				{saving ? 'Saving…' : 'Save'}
			</button>
			{#if successMsg}<span class="success">{successMsg}</span>{/if}
			{#if errorMsg}<span class="error">{errorMsg}</span>{/if}
		</div>
	{/if}
</section>

<style>
	.voice-settings {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 16px;
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
	}
	.sub {
		margin: 4px 0 0;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}
	.muted {
		color: var(--muted-foreground);
		font-size: 0.8125rem;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		gap: 12px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.field .label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}
	.field input,
	.field select {
		padding: 8px 10px;
		border: 1px solid var(--border);
		background: var(--input);
		color: var(--foreground);
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.875rem;
	}
	.pricing,
	.hint {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}
	.toggles {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.toggle {
		display: flex;
		gap: 8px;
		align-items: flex-start;
		font-size: 0.875rem;
		color: var(--foreground);
	}
	.toggle small {
		display: block;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}
	.actions {
		display: flex;
		gap: 12px;
		align-items: center;
		flex-wrap: wrap;
	}
	.btn {
		padding: 8px 16px;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		cursor: pointer;
		font-size: 0.875rem;
		font-weight: 500;
	}
	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.success {
		color: var(--success);
		font-size: 0.8125rem;
	}
	.error {
		color: var(--destructive);
		font-size: 0.8125rem;
	}
</style>
