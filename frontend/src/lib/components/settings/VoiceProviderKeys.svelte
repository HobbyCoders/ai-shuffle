<script lang="ts">
	/**
	 * VoiceProviderKeys — Deepgram / Cartesia / ElevenLabs / Groq key management.
	 *
	 * Rendered inside AIToolsSettings below the fal.ai api-key-card. Reuses
	 * the same card design system (.api-key-card, .input, .btn, .masked-key).
	 */
	import '../deck/cards/card-design-system.css';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';

	type Provider = 'deepgram' | 'cartesia' | 'elevenlabs' | 'groq';

	interface Meta {
		id: Provider;
		name: string;
		hint: string;
		docsUrl: string;
	}

	const PROVIDERS: Meta[] = [
		{
			id: 'deepgram',
			name: 'Deepgram',
			hint: 'Streaming STT (Nova-2)',
			docsUrl: 'https://console.deepgram.com/signup'
		},
		{
			id: 'cartesia',
			name: 'Cartesia',
			hint: 'Streaming TTS (Sonic)',
			docsUrl: 'https://play.cartesia.ai/sign-up'
		},
		{
			id: 'elevenlabs',
			name: 'ElevenLabs',
			hint: 'Premium streaming TTS',
			docsUrl: 'https://elevenlabs.io/app/settings/api-keys'
		},
		{
			id: 'groq',
			name: 'Groq',
			hint: 'Fast Whisper (batch)',
			docsUrl: 'https://console.groq.com/keys'
		}
	];

	interface KeyState {
		input: string;
		masked: string;
		set: boolean;
		saving: boolean;
		removing: boolean;
		success: string;
		error: string;
	}

	const init = (): KeyState => ({
		input: '',
		masked: '',
		set: false,
		saving: false,
		removing: false,
		success: '',
		error: ''
	});

	let keys = $state<Record<Provider, KeyState>>({
		deepgram: init(),
		cartesia: init(),
		elevenlabs: init(),
		groq: init()
	});

	type Settings = Partial<
		Record<`${Provider}_api_key_set` | `${Provider}_api_key_masked`, boolean | string>
	>;

	onMount(async () => {
		try {
			const s = await api.get<Settings>('/settings/integrations');
			for (const p of PROVIDERS) {
				keys[p.id].set = Boolean(s[`${p.id}_api_key_set`] ?? false);
				keys[p.id].masked = (s[`${p.id}_api_key_masked`] as string) || '';
			}
		} catch (e) {
			console.error('Failed to load voice provider keys', e);
		}
	});

	async function saveKey(id: Provider) {
		const st = keys[id];
		if (!st.input.trim()) {
			st.error = 'Enter an API key';
			return;
		}
		st.saving = true;
		st.error = '';
		st.success = '';
		try {
			const r = await api.post<{ success: boolean; masked_key: string; valid: boolean }>(
				`/settings/integrations/voice/${id}`,
				{ api_key: st.input }
			);
			st.masked = r.masked_key;
			st.set = true;
			st.input = '';
			st.success = r.valid ? 'Saved & verified ✓' : 'Saved';
			setTimeout(() => (st.success = ''), 3000);
		} catch (e) {
			st.error = getErrorMessage(e, 'Failed to save');
		} finally {
			st.saving = false;
		}
	}

	async function removeKey(id: Provider) {
		const st = keys[id];
		st.removing = true;
		st.error = '';
		st.success = '';
		try {
			await api.delete(`/settings/integrations/voice/${id}`);
			st.set = false;
			st.masked = '';
			st.success = 'Removed';
			setTimeout(() => (st.success = ''), 2500);
		} catch (e) {
			st.error = getErrorMessage(e, 'Failed to remove');
		} finally {
			st.removing = false;
		}
	}
</script>

<div class="voice-keys-section">
	<div class="voice-keys-header">
		<span class="voice-keys-title">Realtime Voice API Keys</span>
		<span class="voice-keys-sub">
			Optional — powers Pipecat pipeline (Settings → Voice).
		</span>
	</div>

	{#each PROVIDERS as p (p.id)}
		{@const st = keys[p.id]}
		<div class="api-key-card">
			<div class="api-key-header">
				<div class="api-key-title">
					<span class="api-key-label">{p.name}</span>
					{#if st.set}
						<span class="status-dot connected" title="Connected"></span>
					{:else}
						<span class="status-dot disconnected" title="Not Set"></span>
					{/if}
				</div>
				<span class="api-key-hint">{p.hint}</span>
			</div>

			<div class="api-key-body">
				{#if st.masked}
					<code class="masked-key">{st.masked}</code>
				{/if}
				<div class="api-key-input-row">
					<input
						type="password"
						autocomplete="off"
						bind:value={keys[p.id].input}
						placeholder={st.set ? 'Enter new key to replace...' : `Enter your ${p.name} API key`}
						class="input"
						onkeydown={(e) => e.key === 'Enter' && saveKey(p.id)}
					/>
					<button
						onclick={() => saveKey(p.id)}
						disabled={st.saving || !st.input.trim()}
						class="btn primary compact"
						type="button"
					>
						{st.saving ? '...' : 'Save'}
					</button>
					{#if st.set}
						<button
							onclick={() => removeKey(p.id)}
							disabled={st.removing}
							class="btn destructive compact"
							type="button"
						>
							{st.removing ? '...' : 'Remove'}
						</button>
					{/if}
				</div>
				<a
					class="docs-link"
					href={p.docsUrl}
					target="_blank"
					rel="noopener noreferrer"
				>
					Get API key →
				</a>
				{#if st.success}<span class="feedback-success">{st.success}</span>{/if}
				{#if st.error}<span class="feedback-error">{st.error}</span>{/if}
			</div>
		</div>
	{/each}
</div>

<style>
	.voice-keys-section {
		display: flex;
		flex-direction: column;
		gap: 10px;
		margin-top: 6px;
	}

	.voice-keys-header {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 10px 14px;
		background: color-mix(in oklch, var(--primary) 6%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 18%, transparent);
		border-radius: calc(var(--radius) - 2px);
	}

	.voice-keys-title {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.voice-keys-sub {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.docs-link {
		align-self: flex-start;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-decoration: none;
	}

	.docs-link:hover {
		color: var(--foreground);
	}
</style>
