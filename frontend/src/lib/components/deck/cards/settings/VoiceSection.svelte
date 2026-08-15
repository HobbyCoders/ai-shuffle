<script lang="ts">
	/**
	 * VoiceSection — Settings → Voice (ruling VM17).
	 *
	 * Runtime-editable knobs backed by `GET/PUT /api/v1/settings/voice`:
	 * the enable flag, the realtime model, the agent's voice (with per-voice
	 * preview playback, VM21), which OpenAI API-key provider row funds calls,
	 * and a cap on total call length. Load once, PUT on change — the PUT is a
	 * partial update, and an explicit `null` provider clears the pin.
	 *
	 * Two traps this section is written around:
	 *
	 * * A settings section under `{#if activeTab === …}` REMOUNTS on every tab
	 *   switch, so it must not re-fetch shared state on mount. `loadVoiceSettings`
	 *   is memoised at module scope, so the mount read is free after the first.
	 * * The one free-form field (max call minutes) is one-way bound, so a fetch
	 *   resolving mid-typing would snap it back. `pendingMinutes` holds the local
	 *   edit and is the only thing rendered while an edit is outstanding; the
	 *   server value is applied on commit (blur / Enter), never underneath the
	 *   caret.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { AudioLines, Play, Square } from 'lucide-svelte';
	import {
		VOICE_MODELS,
		VOICE_VOICES,
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
	let error = $state('');
	let success = $state('');

	// Free-text guard: non-null means the user is mid-edit and owns the field.
	let pendingMinutes = $state<string | null>(null);
	const minutesValue = $derived(
		pendingMinutes ?? String(settings?.voice_max_call_minutes ?? '')
	);

	onMount(async () => {
		try {
			settings = await loadVoiceSettings();
		} catch (e: any) {
			error = e?.detail || 'Failed to load voice settings';
		} finally {
			loading = false;
		}
		try {
			providers = await getVoiceProviders();
		} catch (e) {
			// Non-fatal: the picker just shows its setup pointer.
			console.warn('[voice] could not list eligible providers', e);
		}
	});

	async function save(patch: Parameters<typeof saveVoiceSettings>[0]) {
		saving = true;
		error = '';
		success = '';
		try {
			settings = await saveVoiceSettings(patch);
			success = 'Saved';
			setTimeout(() => (success = ''), 2000);
		} catch (e: any) {
			error = e?.detail || 'Failed to save voice settings';
		} finally {
			saving = false;
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

	// ---- Agent prompts (voice agent config) -------------------------------
	// Three editable prompts with the same mid-edit guard as the minutes
	// field: a non-null pending value means the user owns the textarea. Empty
	// saves clear the override (server falls back to the built-in default,
	// shown as the placeholder).
	type PromptField = 'voice_instructions' | 'voice_instructions_command' | 'voice_instructions_chat';
	const PROMPTS: Array<{
		field: PromptField;
		defaultKey: 'base' | 'command' | 'chat';
		title: string;
		blurb: string;
	}> = [
		{
			field: 'voice_instructions',
			defaultKey: 'base',
			title: 'Agent instructions',
			blurb: 'The voice agent’s system prompt — personality and ground rules for every voice call (web and phone).'
		},
		{
			field: 'voice_instructions_command',
			defaultKey: 'command',
			title: 'Hands-free (wake word) additions',
			blurb: 'Appended when the phone app starts a session by wake word: one-shot tasks, hang up when done.'
		},
		{
			field: 'voice_instructions_chat',
			defaultKey: 'chat',
			title: 'Conversation additions',
			blurb: 'Appended when the phone app starts a session from the button, tile, or assist gesture.'
		}
	];
	let pendingPrompts = $state<Record<string, string | null>>({});

	function promptValue(field: PromptField): string {
		return pendingPrompts[field] ?? settings?.[field] ?? '';
	}

	function commitPrompt(field: PromptField) {
		const raw = pendingPrompts[field];
		pendingPrompts[field] = null;
		if (raw === null || raw === undefined) return;
		const text = raw.trim();
		if (text === (settings?.[field] ?? '')) return;
		void save({ [field]: text });
	}

	function resetPrompt(field: PromptField) {
		pendingPrompts[field] = null;
		if (!settings?.[field]) return;
		void save({ [field]: '' });
	}

	// ---- Voice preview (VM21) ---------------------------------------------
	// One shared <audio> element; starting a preview stops whatever was playing.
	// Fetched blobs are cached per voice so replays don't re-bill the TTS call.
	let previewingVoice = $state<string | null>(null);
	let loadingPreview = $state<string | null>(null);
	let previewAudio: HTMLAudioElement | null = null;
	const previewCache = new Map<string, string>();

	function stopPreview() {
		previewAudio?.pause();
		previewAudio = null;
		previewingVoice = null;
	}

	async function playPreview(voiceId: string) {
		if (previewingVoice === voiceId) {
			stopPreview();
			return;
		}
		stopPreview();
		if (loadingPreview) return;
		error = '';
		try {
			let url = previewCache.get(voiceId);
			if (!url) {
				loadingPreview = voiceId;
				const blob = await previewVoice(voiceId);
				url = URL.createObjectURL(blob);
				previewCache.set(voiceId, url);
			}
			const audio = new Audio(url);
			audio.onended = () => {
				if (previewAudio === audio) stopPreview();
			};
			previewAudio = audio;
			previewingVoice = voiceId;
			await audio.play();
		} catch (e: any) {
			stopPreview();
			error = e?.detail || 'Could not play the voice preview.';
		} finally {
			loadingPreview = null;
		}
	}

	onDestroy(() => {
		stopPreview();
		for (const url of previewCache.values()) URL.revokeObjectURL(url);
		previewCache.clear();
	});
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><AudioLines />Voice</h3>
		<p>
			Talk to an agent that dispatches the work to other agents. Runs on OpenAI Realtime over a
			direct browser connection — audio never passes through this server.
		</p>
	</div>

	{#if loading}
		<div class="set-row"><div class="lab"><p>Loading voice settings…</p></div></div>
	{:else if !settings}
		<div class="set-row"><div class="lab"><p class="msg err">{error || 'Voice settings unavailable.'}</p></div></div>
	{:else}
		<div class="set-row">
			<div class="lab">
				<h4>Enable voice mode</h4>
				<p>Adds a Voice option to the new-chat controls.</p>
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

		<div class="set-row stack">
			<div class="lab">
				<h4>Realtime model</h4>
				<p>Which speech-to-speech model answers. Cost is per minute of conversation.</p>
			</div>
			<div class="ctl">
				<div class="model-list" role="radiogroup" aria-label="Realtime model">
					{#each VOICE_MODELS as model}
						<label class="model-opt" class:on={settings.voice_model === model.id}>
							<input
								type="radio"
								name="voice-model"
								value={model.id}
								checked={settings.voice_model === model.id}
								disabled={saving}
								onchange={() => save({ voice_model: model.id })}
							/>
							<span>{model.label}</span>
						</label>
					{/each}
				</div>
			</div>
		</div>

		<div class="set-row stack">
			<div class="lab">
				<h4>Voice</h4>
				<p>How the agent sounds. Press play to hear a short sample of each voice.</p>
			</div>
			<div class="ctl">
				<div class="voice-list" role="radiogroup" aria-label="Agent voice">
					{#each VOICE_VOICES as voice}
						<label class="voice-opt" class:on={settings.voice_voice === voice.id}>
							<input
								type="radio"
								name="voice-voice"
								value={voice.id}
								checked={settings.voice_voice === voice.id}
								disabled={saving}
								onchange={() => save({ voice_voice: voice.id })}
							/>
							<span class="voice-name">{voice.label}</span>
							<button
								type="button"
								class="preview-btn"
								class:playing={previewingVoice === voice.id}
								disabled={loadingPreview !== null && loadingPreview !== voice.id}
								aria-label={previewingVoice === voice.id
									? `Stop the ${voice.id} preview`
									: `Preview the ${voice.id} voice`}
								title={previewingVoice === voice.id ? 'Stop preview' : 'Preview this voice'}
								onclick={(e) => {
									// A click on the play button must not toggle the radio.
									e.preventDefault();
									e.stopPropagation();
									void playPreview(voice.id);
								}}
							>
								{#if loadingPreview === voice.id}
									<span class="preview-spinner" aria-hidden="true"></span>
								{:else if previewingVoice === voice.id}
									<Square size={11} strokeWidth={2} />
								{:else}
									<Play size={11} strokeWidth={2} />
								{/if}
							</button>
						</label>
					{/each}
				</div>
			</div>
		</div>

		<div class="set-row stack">
			<div class="lab">
				<h4>Provider</h4>
				<p>The OpenAI API-key row that funds calls.</p>
			</div>
			<div class="ctl">
				{#if providers.length === 0}
					<p class="msg err">
						Add an OpenAI API-key provider in Settings → Providers (ChatGPT logins can't do
						Realtime)
					</p>
				{:else}
					<select
						class="sel"
						value={settings.voice_provider_id ?? ''}
						disabled={saving}
						onchange={(e) => save({ voice_provider_id: e.currentTarget.value || null })}
					>
						<option value="">Automatic (use the only eligible provider)</option>
						{#each providers as provider}
							<option value={provider.id}>{provider.name}</option>
						{/each}
					</select>
				{/if}
			</div>
		</div>

		{#each PROMPTS as prompt}
			<div class="set-row stack">
				<div class="lab">
					<h4>{prompt.title}</h4>
					<p>{prompt.blurb}</p>
				</div>
				<div class="ctl">
					<textarea
						class="inp prompt"
						rows="5"
						value={promptValue(prompt.field)}
						placeholder={settings.voice_instructions_defaults?.[prompt.defaultKey] ?? ''}
						disabled={saving}
						oninput={(e) => (pendingPrompts[prompt.field] = e.currentTarget.value)}
						onblur={() => commitPrompt(prompt.field)}
						aria-label={prompt.title}
					></textarea>
					{#if settings[prompt.field]}
						<button
							type="button"
							class="reset-btn"
							disabled={saving}
							onclick={() => resetPrompt(prompt.field)}
						>Reset to default</button>
					{:else}
						<p class="msg dim">Using the built-in default (shown greyed above). Type to override.</p>
					{/if}
				</div>
			</div>
		{/each}

		<div class="set-row">
			<div class="lab">
				<h4>Max call minutes</h4>
				<p>Total length cap for one call. OpenAI's own 60-minute session wall is bridged automatically.</p>
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
	.model-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.model-opt {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 7px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		font-size: 12.5px;
		color: var(--foreground);
		cursor: pointer;
	}
	.model-opt.on {
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}
	.minutes {
		width: 110px;
	}

	/* Voice agent prompt editors */
	.prompt {
		width: 100%;
		min-height: 96px;
		resize: vertical;
		font-size: 12.5px;
		line-height: 1.45;
		font-family: inherit;
	}
	.reset-btn {
		margin-top: 6px;
		padding: 4px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		background: var(--background);
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
	}
	.reset-btn:hover:not(:disabled) {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
	}
	.msg.dim {
		color: var(--muted-foreground);
		margin-top: 6px;
	}

	/* Voice picker (VM21) — two-column grid of compact options, each carrying
	   its own preview control. */
	.voice-list {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 6px;
	}
	.voice-opt {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px 6px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		font-size: 12.5px;
		color: var(--foreground);
		cursor: pointer;
	}
	.voice-opt.on {
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}
	.voice-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.preview-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		flex-shrink: 0;
		border: 1px solid var(--border);
		border-radius: 50%;
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
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
		width: 10px;
		height: 10px;
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
