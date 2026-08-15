<script lang="ts">
	/**
	 * VoiceSection — Settings → Voice (ruling VM17).
	 *
	 * Four runtime-editable knobs backed by `GET/PUT /api/v1/settings/voice`:
	 * the enable flag, the realtime model, which OpenAI API-key provider row
	 * funds calls, and a cap on total call length. Load once, PUT on change —
	 * the PUT is a partial update, and an explicit `null` provider clears the
	 * pin.
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
	import { onMount } from 'svelte';
	import { AudioLines } from 'lucide-svelte';
	import {
		VOICE_MODELS,
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
