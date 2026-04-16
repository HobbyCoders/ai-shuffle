<script lang="ts">
	/**
	 * ApiKeySettings - API key management for fal.ai + realtime voice providers.
	 *
	 * Providers:
	 *   - fal.ai      — image/video/3D generation + batch TTS/STT
	 *   - Deepgram    — streaming STT (Pipecat realtime pipeline)
	 *   - Cartesia    — streaming TTS (Pipecat realtime pipeline)
	 *   - ElevenLabs  — premium streaming TTS (upgrade path)
	 *   - Groq        — fast Whisper STT fallback
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';

	type VoiceProvider = 'deepgram' | 'cartesia' | 'elevenlabs' | 'groq';

	interface ProviderMeta {
		id: VoiceProvider;
		name: string;
		description: string;
		docsUrl: string;
	}

	const VOICE_PROVIDERS: ProviderMeta[] = [
		{
			id: 'deepgram',
			name: 'Deepgram API Key',
			description: 'Streaming speech-to-text (Nova-2). ~100–300ms latency, interim transcripts.',
			docsUrl: 'https://console.deepgram.com/signup'
		},
		{
			id: 'cartesia',
			name: 'Cartesia API Key',
			description: 'Streaming text-to-speech (Sonic). ~90ms first-byte, token-level streaming.',
			docsUrl: 'https://play.cartesia.ai/sign-up'
		},
		{
			id: 'elevenlabs',
			name: 'ElevenLabs API Key',
			description: 'Premium streaming TTS (Turbo v2.5). Upgrade path from Cartesia.',
			docsUrl: 'https://elevenlabs.io/app/settings/api-keys'
		},
		{
			id: 'groq',
			name: 'Groq API Key',
			description: 'Fast Whisper STT via LPU. Batch fallback alternative to Deepgram streaming.',
			docsUrl: 'https://console.groq.com/keys'
		}
	];

	// -------- fal.ai state (existing) --------
	let falExpanded = $state(true);
	let falApiKey = $state('');
	let falApiKeyMasked = $state('');
	let falApiKeySet = $state(false);
	let savingFalKey = $state(false);
	let removingFalKey = $state(false);
	let falKeySuccess = $state('');
	let falKeyError = $state('');

	// -------- voice provider state (generic per-provider record) --------
	interface VoiceKeyState {
		expanded: boolean;
		input: string;
		masked: string;
		set: boolean;
		saving: boolean;
		removing: boolean;
		success: string;
		error: string;
	}

	function initVoiceState(): VoiceKeyState {
		return {
			expanded: false,
			input: '',
			masked: '',
			set: false,
			saving: false,
			removing: false,
			success: '',
			error: ''
		};
	}

	let voiceKeys = $state<Record<VoiceProvider, VoiceKeyState>>({
		deepgram: initVoiceState(),
		cartesia: initVoiceState(),
		elevenlabs: initVoiceState(),
		groq: initVoiceState()
	});

	type IntegrationSettings = {
		fal_api_key_set?: boolean;
		fal_api_key_masked?: string;
		deepgram_api_key_set?: boolean;
		deepgram_api_key_masked?: string;
		cartesia_api_key_set?: boolean;
		cartesia_api_key_masked?: string;
		elevenlabs_api_key_set?: boolean;
		elevenlabs_api_key_masked?: string;
		groq_api_key_set?: boolean;
		groq_api_key_masked?: string;
	};

	onMount(() => {
		loadIntegrationSettings();
	});

	async function loadIntegrationSettings() {
		try {
			const settings = await api.get<IntegrationSettings>('/settings/integrations');

			falApiKeySet = settings.fal_api_key_set ?? false;
			falApiKeyMasked = settings.fal_api_key_masked || '';

			for (const p of VOICE_PROVIDERS) {
				const setKey = `${p.id}_api_key_set` as keyof IntegrationSettings;
				const maskedKey = `${p.id}_api_key_masked` as keyof IntegrationSettings;
				voiceKeys[p.id].set = Boolean(settings[setKey] ?? false);
				voiceKeys[p.id].masked = (settings[maskedKey] as string) || '';
			}
		} catch (e) {
			console.error('Failed to load integration settings:', e);
		}
	}

	// -------- fal.ai handlers (unchanged) --------
	async function saveFalApiKey() {
		if (!falApiKey.trim()) {
			falKeyError = 'Please enter an API key';
			return;
		}
		savingFalKey = true;
		falKeyError = '';
		falKeySuccess = '';
		try {
			const result = await api.post<{ success: boolean; masked_key: string; valid: boolean }>(
				'/settings/integrations/fal-ai',
				{ api_key: falApiKey }
			);
			falApiKeyMasked = result.masked_key;
			falApiKeySet = true;
			falApiKey = '';
			falKeySuccess = result.valid ? 'Key saved & verified ✓' : 'Key saved (could not verify)';
			setTimeout(() => (falKeySuccess = ''), 4000);
		} catch (e: unknown) {
			falKeyError = getErrorMessage(e, 'Failed to save');
		} finally {
			savingFalKey = false;
		}
	}

	async function removeFalApiKey() {
		removingFalKey = true;
		falKeyError = '';
		falKeySuccess = '';
		try {
			await api.delete('/settings/integrations/fal-ai');
			falApiKeySet = false;
			falApiKeyMasked = '';
			falKeySuccess = 'Key removed';
			setTimeout(() => (falKeySuccess = ''), 3000);
		} catch (e: unknown) {
			falKeyError = getErrorMessage(e, 'Failed to remove');
		} finally {
			removingFalKey = false;
		}
	}

	// -------- voice provider handlers (generic) --------
	async function saveVoiceKey(provider: VoiceProvider) {
		const s = voiceKeys[provider];
		if (!s.input.trim()) {
			s.error = 'Please enter an API key';
			return;
		}
		s.saving = true;
		s.error = '';
		s.success = '';
		try {
			const result = await api.post<{ success: boolean; masked_key: string; valid: boolean }>(
				`/settings/integrations/voice/${provider}`,
				{ api_key: s.input }
			);
			s.masked = result.masked_key;
			s.set = true;
			s.input = '';
			s.success = result.valid ? 'Key saved & verified ✓' : 'Key saved (could not verify)';
			setTimeout(() => (s.success = ''), 4000);
		} catch (e: unknown) {
			s.error = getErrorMessage(e, 'Failed to save');
		} finally {
			s.saving = false;
		}
	}

	async function removeVoiceKey(provider: VoiceProvider) {
		const s = voiceKeys[provider];
		s.removing = true;
		s.error = '';
		s.success = '';
		try {
			await api.delete(`/settings/integrations/voice/${provider}`);
			s.set = false;
			s.masked = '';
			s.success = 'Key removed';
			setTimeout(() => (s.success = ''), 3000);
		} catch (e: unknown) {
			s.error = getErrorMessage(e, 'Failed to remove');
		} finally {
			s.removing = false;
		}
	}
</script>

<!-- fal.ai (primary, keeps original structure + behavior) -->
<section class="section">
	<button class="section-header" onclick={() => (falExpanded = !falExpanded)}>
		<h3>fal.ai API Key</h3>
		<div class="header-right">
			{#if falApiKeySet}
				<span class="status-badge connected">Connected</span>
			{:else}
				<span class="status-badge disconnected">Not Set</span>
			{/if}
			<svg
				class="chevron"
				class:expanded={falExpanded}
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
			>
				<polyline points="6 9 12 15 18 9" />
			</svg>
		</div>
	</button>
	{#if falExpanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<p class="description">
				One API key powers all AI tools — image, video, 3D, and audio generation.
			</p>

			<div class="api-key-row">
				{#if falApiKeyMasked}
					<div class="api-key-info">
						<code class="masked-key">{falApiKeyMasked}</code>
					</div>
				{/if}
				<div class="api-key-input">
					<input
						type="password"
						bind:value={falApiKey}
						placeholder={falApiKeySet ? 'Enter new key to replace...' : 'Enter your fal.ai API key'}
						class="input"
						onkeydown={(e) => e.key === 'Enter' && saveFalApiKey()}
					/>
					<button
						onclick={saveFalApiKey}
						disabled={savingFalKey || !falApiKey.trim()}
						class="btn primary compact"
					>
						{savingFalKey ? '...' : 'Save'}
					</button>
					{#if falApiKeySet}
						<button
							onclick={removeFalApiKey}
							disabled={removingFalKey}
							class="btn destructive compact"
						>
							{removingFalKey ? '...' : 'Remove'}
						</button>
					{/if}
				</div>
			</div>

			{#if falKeySuccess}<div class="success-msg">{falKeySuccess}</div>{/if}
			{#if falKeyError}<div class="error-msg">{falKeyError}</div>{/if}
		</div>
	{/if}
</section>

<!-- Pipecat intro banner -->
<div class="voice-banner">
	<strong>Realtime voice</strong> (below) uses the Pipecat pipeline — Deepgram for streaming STT,
	Cartesia or ElevenLabs for streaming TTS, Groq for fast Whisper fallback. All keys are optional;
	configure only the providers you want to use.
</div>

<!-- Voice provider sections (generic loop) -->
{#each VOICE_PROVIDERS as provider (provider.id)}
	{@const state = voiceKeys[provider.id]}
	<section class="section">
		<button
			class="section-header"
			onclick={() => (voiceKeys[provider.id].expanded = !state.expanded)}
		>
			<h3>{provider.name}</h3>
			<div class="header-right">
				{#if state.set}
					<span class="status-badge connected">Connected</span>
				{:else}
					<span class="status-badge optional">Optional</span>
				{/if}
				<svg
					class="chevron"
					class:expanded={state.expanded}
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
				>
					<polyline points="6 9 12 15 18 9" />
				</svg>
			</div>
		</button>
		{#if state.expanded}
			<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
				<p class="description">{provider.description}</p>

				<div class="api-key-row">
					{#if state.masked}
						<div class="api-key-info">
							<code class="masked-key">{state.masked}</code>
						</div>
					{/if}
					<div class="api-key-input">
						<input
							type="password"
							bind:value={voiceKeys[provider.id].input}
							placeholder={state.set
								? 'Enter new key to replace...'
								: `Enter your ${provider.name.replace(' API Key', '')} API key`}
							class="input"
							onkeydown={(e) => e.key === 'Enter' && saveVoiceKey(provider.id)}
						/>
						<button
							onclick={() => saveVoiceKey(provider.id)}
							disabled={state.saving || !state.input.trim()}
							class="btn primary compact"
						>
							{state.saving ? '...' : 'Save'}
						</button>
						{#if state.set}
							<button
								onclick={() => removeVoiceKey(provider.id)}
								disabled={state.removing}
								class="btn destructive compact"
							>
								{state.removing ? '...' : 'Remove'}
							</button>
						{/if}
					</div>
					<a
						class="docs-link"
						href={provider.docsUrl}
						target="_blank"
						rel="noopener noreferrer"
					>
						Get API key →
					</a>
				</div>

				{#if state.success}<div class="success-msg">{state.success}</div>{/if}
				{#if state.error}<div class="error-msg">{state.error}</div>{/if}
			</div>
		{/if}
	</section>
{/each}

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

	.section-header:hover {
		background: var(--hover-overlay);
	}
	.section-header:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.header-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded {
		transform: rotate(180deg);
	}

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.description {
		margin: 0;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.5;
	}

	.api-key-row {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.api-key-info {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.masked-key {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: var(--secondary);
		padding: 2px 6px;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
	}

	.api-key-input {
		display: flex;
		gap: 6px;
	}

	.api-key-input .input {
		flex: 1;
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

	.input::placeholder {
		color: var(--muted-foreground);
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

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn.primary:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn.destructive {
		background: var(--destructive);
		color: var(--destructive-foreground, #fff);
	}

	.btn.destructive:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn.compact {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	.status-badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.025em;
	}

	.status-badge.connected {
		background: color-mix(in oklch, var(--success, #22c55e) 15%, transparent);
		color: var(--success, #22c55e);
		border: 1px solid color-mix(in oklch, var(--success, #22c55e) 30%, transparent);
	}

	.status-badge.disconnected {
		background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
		color: var(--muted-foreground);
		border: 1px solid color-mix(in oklch, var(--muted-foreground) 30%, transparent);
	}

	.status-badge.optional {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--muted-foreground);
		border: 1px solid color-mix(in oklch, var(--primary) 25%, transparent);
	}

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}

	.voice-banner {
		margin: 12px 0 0;
		padding: 10px 14px;
		background: color-mix(in oklch, var(--primary) 6%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 18%, transparent);
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		line-height: 1.5;
	}

	.voice-banner strong {
		color: var(--foreground);
	}

	.docs-link {
		align-self: flex-start;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-decoration: none;
		transition: color 150ms ease;
	}

	.docs-link:hover {
		color: var(--foreground);
	}
</style>
