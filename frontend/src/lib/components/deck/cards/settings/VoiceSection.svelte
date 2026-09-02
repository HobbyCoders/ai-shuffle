<script lang="ts">
	/**
	 * VoiceSection — Settings → Voice (ruling VM17).
	 *
	 * Runtime-editable knobs backed by `GET/PUT /api/v1/settings/voice`:
	 * the enable flag, the realtime model, the agent's voice (with a preview
	 * of the selected one, VM21), which OpenAI API-key provider row funds
	 * calls, whether the agent may manage app configuration directly, and a
	 * cap on total call length. Load once, PUT on change — the PUT is a
	 * partial update, and an explicit `null` provider clears the pin.
	 *
	 * The section also shows which agents a call can dispatch. That roster is
	 * NOT edited here: an agent joins it by getting a model under its own
	 * editor (Agents → Provider models → Voice), so this is a read-only mirror
	 * with a pointer to the place that configures it.
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
	import { AudioLines, Bot, Play, Square, Wrench } from 'lucide-svelte';
	import {
		VOICE_MODELS,
		VOICE_VOICES,
		getVoiceAgents,
		getVoiceProviders,
		loadVoiceSettings,
		previewVoice,
		saveVoiceSettings,
		type VoiceAgent,
		type VoiceProvider,
		type VoiceSettings,
	} from '$lib/api/voice';

	let settings = $state<VoiceSettings | null>(null);
	let providers = $state<VoiceProvider[]>([]);
	let agents = $state<VoiceAgent[]>([]);
	let agentsLoaded = $state(false);
	let loading = $state(true);
	let saving = $state(false);
	let error = $state('');
	let success = $state('');

	// Free-text guard: non-null means the user is mid-edit and owns the field.
	let pendingMinutes = $state<string | null>(null);
	const minutesValue = $derived(
		pendingMinutes ?? String(settings?.voice_max_call_minutes ?? '')
	);

	const selectedModel = $derived(VOICE_MODELS.find((m) => m.id === settings?.voice_model));
	const selectedVoice = $derived(VOICE_VOICES.find((v) => v.id === settings?.voice_voice));

	onMount(async () => {
		try {
			settings = await loadVoiceSettings();
		} catch (e: any) {
			error = e?.detail || 'Failed to load voice settings';
		} finally {
			loading = false;
		}
		// Both lists are non-fatal: a failed read just shows the setup pointer.
		const [providerList, agentList] = await Promise.allSettled([
			getVoiceProviders(),
			getVoiceAgents(),
		]);
		if (providerList.status === 'fulfilled') providers = providerList.value;
		else console.warn('[voice] could not list eligible providers', providerList.reason);
		if (agentList.status === 'fulfilled') agents = agentList.value;
		else console.warn('[voice] could not list voice agents', agentList.reason);
		agentsLoaded = true;
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
		stopPreview();
		for (const url of previewCache.values()) URL.revokeObjectURL(url);
		previewCache.clear();
	});
</script>

<div class="set-group" data-testid="voice-section">
	<div class="set-grouphead">
		<h3><AudioLines />Voice</h3>
		<p>
			Talk to an agent that hands the work to your other agents. Runs on OpenAI Realtime over a
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
				<p>Adds a call button next to the send button in every new chat.</p>
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
				<p>The OpenAI API-key provider that funds calls.</p>
			</div>
			<div class="ctl">
				{#if providers.length === 0}
					<p class="msg err">
						Add an OpenAI API-key provider in Settings → Providers. ChatGPT logins can't do
						Realtime.
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
				<p>{selectedModel?.hint ?? 'Which speech-to-speech model answers. Billed per minute of conversation.'}</p>
			</div>
			<div class="ctl">
				<select
					class="sel"
					value={settings.voice_model}
					disabled={saving}
					aria-label="Realtime model"
					onchange={(e) => save({ voice_model: e.currentTarget.value })}
				>
					{#each VOICE_MODELS as model}
						<option value={model.id}>{model.label}</option>
					{/each}
				</select>
			</div>
		</div>

		<div class="set-row">
			<div class="lab">
				<h4>Voice</h4>
				<p>{selectedVoice?.hint ?? 'How the agent sounds.'} Press play to hear a sample.</p>
			</div>
			<div class="ctl">
				<select
					class="sel"
					value={settings.voice_voice}
					disabled={saving}
					aria-label="Agent voice"
					onchange={(e) => changeVoice(e.currentTarget.value)}
				>
					{#each VOICE_VOICES as voice}
						<option value={voice.id}>{voice.label}</option>
					{/each}
				</select>
				<button
					type="button"
					class="preview-btn"
					class:playing={previewing}
					disabled={saving || (loadingPreview && !previewing)}
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

{#if settings}
	<div class="set-group" data-testid="voice-agent-section">
		<div class="set-grouphead">
			<h3><Bot />What the voice agent can do</h3>
			<p>
				The voice agent never edits files or runs commands itself. It hands that work to the
				agents below and keeps you posted — and, optionally, manages the app directly.
			</p>
		</div>

		<div class="set-row">
			<div class="lab">
				<h4><Wrench class="inline-icon" />Manage app configuration by voice</h4>
				<p>
					Let the agent create and change automations, hooks, agents, MCP servers and connectors
					during a call. Off: it can only dispatch work and report on it.
				</p>
			</div>
			<div class="ctl">
				<button
					type="button"
					class="switch"
					class:on={settings.voice_app_tools_enabled}
					role="switch"
					aria-checked={settings.voice_app_tools_enabled}
					aria-label="Manage app configuration by voice"
					disabled={saving}
					onclick={() => save({ voice_app_tools_enabled: !settings?.voice_app_tools_enabled })}
				><i></i></button>
			</div>
		</div>

		<div class="set-row stack">
			<div class="lab">
				<h4>Agents available in calls</h4>
				<p>
					An agent joins this list when it has a model under
					<strong>Agents → edit agent → Provider models → Voice</strong>. The agent you start the
					call with is used by default; the rest can be asked for by name.
				</p>
			</div>
			<div class="ctl">
				{#if !agentsLoaded}
					<p class="msg dim">Loading agents…</p>
				{:else if agents.length === 0}
					<p class="msg warn">
						No agent has a voice model yet, so the voice agent can't dispatch any work. Open an
						agent and set one under Provider models → Voice.
					</p>
				{:else}
					<ul class="agent-list">
						{#each agents as agent (agent.id)}
							<li class="agent-row">
								<span class="agent-name">{agent.name}</span>
								{#if agent.description}<span class="agent-desc">{agent.description}</span>{/if}
								<span class="agent-leg" title={agent.model}>{agent.provider_name} · {agent.model}</span>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</div>
	</div>
{/if}

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

	:global(.inline-icon) {
		width: 14px;
		height: 14px;
		margin-right: 6px;
		vertical-align: -2px;
		color: var(--muted-foreground);
	}

	/* Read-only roster of voice-dispatchable agents. */
	.agent-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.agent-row {
		display: flex;
		align-items: baseline;
		gap: 10px;
		min-width: 0;
		padding: 7px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		font-size: 12.5px;
	}
	.agent-name {
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
	}
	.agent-desc {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--muted-foreground);
	}
	.agent-leg {
		margin-left: auto;
		flex-shrink: 0;
		max-width: 45%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		color: var(--muted-foreground);
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
	.msg.warn {
		color: var(--warning, #d97706);
	}
	.msg.dim {
		color: var(--muted-foreground);
	}
</style>
