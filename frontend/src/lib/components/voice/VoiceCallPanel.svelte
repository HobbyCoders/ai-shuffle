<script lang="ts">
	/** Session-scoped call status and controls. Compact mode sits beside the
	 * persistent text composer; unmounting this view never ends the call. */
	import { onDestroy, untrack } from 'svelte';
	import VoiceVolume from './VoiceVolume.svelte';
	import { Mic, MicOff, PhoneOff, Volume2, VolumeX } from 'lucide-svelte';
	import { voiceCallState, hangup, toggleAgent, toggleMic, MIC_PROBLEM_LABEL } from '$lib/services/voiceCall';
	import { getVoiceCallState, voiceModelLabel, usesDelegation, usesAudioStream } from '$lib/api/voice';

	interface Props {
		sessionId: string;
		compact?: boolean;
	}

	let { sessionId, compact = false }: Props = $props();

	const call = $derived($voiceCallState);

	// The controller is a singleton: only reflect it when it is pointed at THIS
	// session, so switching to another voice session doesn't show a stale call.
	const mine = $derived(call.sessionId === sessionId);
	const status = $derived(mine ? call.status : 'idle');
	const live = $derived(status === 'live' || status === 'reconnecting');
	const boundModel = $derived(mine ? call.model : null);
	const delegated = $derived(mine && usesDelegation(call.model, call.options));
	const realtime = $derived(mine && !!call.model && !usesAudioStream(call.model));
	const engineLabel = $derived(mine && call.model ? voiceModelLabel(call.model) : 'Voice agent');
	const modeLabel = $derived(delegated ? 'Conversation + your chat agent' : call.model === 'gpt-live-1' ? 'OpenAI Responses delegation' : realtime ? (call.options?.turn_detection === 'server_vad' ? 'Realtime agent tools · Normal VAD' : 'Realtime agent tools') : 'Full-duplex conversation');

	// Never show a healthy call while the microphone is not reaching it (#1824).
	const micProblem = $derived(status === 'live' && !call.micMuted && call.micProblem);
	const statusLabel = $derived(
		status === 'live'
			? call.micMuted
				? 'Mic muted'
				: call.micProblem
					? MIC_PROBLEM_LABEL
					: call.agentSpeaking
					? 'Speaking…'
					: call.userSpeaking
						? 'Hearing you…'
						: realtime || call.model === 'gpt-live-1' ? 'Listening' : 'Audio connected'
			: status === 'connecting'
				? 'Connecting…'
				: status === 'reconnecting'
					? 'Reconnecting…'
					: status === 'ended'
						? 'Call ended'
						: status === 'error'
							? 'Call failed'
							: 'No call running'
	);

	// Elapsed timer — ticks only while a call is up.
	let now = $state(Date.now());
	let ticker: ReturnType<typeof setInterval> | null = null;
	$effect(() => {
		if (live && call.startedAt) {
			now = Date.now();
			ticker ??= setInterval(() => (now = Date.now()), 1000);
		} else if (ticker) {
			clearInterval(ticker);
			ticker = null;
		}
	});
	// Server-side call state (VW5) — the spend estimate (only the server sees
	// `response.done` usage) and how many worker agents are running. Polled
	// slowly and best-effort: a glanceable status line, not a meter.
	const COST_POLL_MS = 10_000;
	let costUsd = $state<number | null>(null);
	let activeTasks = $state(0);
	let costTimer: ReturnType<typeof setInterval> | null = null;

	async function pollCost() {
		try {
			const id = sessionId;
			const model = call.model;
			const state = await getVoiceCallState(id);
			if (!mine || !live || sessionId !== id || call.model !== model || !realtime) return;
			costUsd = state?.estimated_cost_usd ?? null;
			activeTasks = state?.active_tasks ?? 0;
		} catch {
			/* a failed poll just leaves the last estimate on screen */
		}
	}

	$effect(() => {
		// Only binding/status changes restart telemetry, never microphone levels.
		void boundModel;
		void sessionId;
		costUsd = null;
		activeTasks = 0;
		if (live && realtime) {
			untrack(() => void pollCost());
			costTimer = setInterval(() => void pollCost(), COST_POLL_MS);
		}
		return () => {
			if (costTimer) clearInterval(costTimer);
			costTimer = null;
		};
	});

	/** "~$0.12" once there is something to show; "<$0.01" while it is tiny. */
	const costLabel = $derived(
		costUsd === null || costUsd <= 0 ? '' : costUsd >= 0.01 ? `~$${costUsd.toFixed(2)}` : '<$0.01'
	);

	onDestroy(() => {
		if (ticker) clearInterval(ticker);
		if (costTimer) clearInterval(costTimer);
	});

	const elapsed = $derived.by(() => {
		if (!live || !call.startedAt) return '';
		const total = Math.max(0, Math.floor((now - call.startedAt) / 1000));
		const m = Math.floor(total / 60);
		const s = total % 60;
		return `${m}:${String(s).padStart(2, '0')}`;
	});

	// Orb scale follows the mic level; muted or idle sits still. While the
	// agent talks the orb breathes on its own (CSS) instead of following the mic.
	const orbScale = $derived(live && !call.micMuted ? 1 + Math.min(call.level, 1) * 0.55 : 1);
	const workingLabel = $derived(
		activeTasks === 1 ? '1 agent working' : activeTasks > 1 ? `${activeTasks} agents working` : ''
	);
</script>

<div class="voice-dock-wrapper" class:compact data-testid="voice-call-dock">
	{#if mine && call.error}<p class="vp-msg err" role="alert">{call.error}</p>{/if}
	{#if mine && call.notice}<p class="vp-msg warn">{call.notice}</p>{/if}
	<div class="voice-ring" class:live class:speaking={live && call.agentSpeaking} class:hearing={live && call.userSpeaking} data-testid="voice-ring" title={`${engineLabel} · ${modeLabel}`}>
		<h3 class="sr-only">{engineLabel}</h3>
		<p class="sr-only">{modeLabel}</p>
		<div class="ring-track" aria-hidden="true"></div>
		<div class="ring-halo" aria-hidden="true" style={`--mic-scale: ${orbScale};`}></div>
		<div class="ring-center" aria-hidden="true">
			<div class="waveform">{#each [0, 1, 2, 3, 4] as bar}<i style={`--bar: ${bar};`}></i>{/each}</div>
			{#if elapsed}<span class="vp-elapsed">{elapsed}</span>{/if}
		</div>
		<div class="vp-controls" role="group" aria-label="Voice call controls">
			<button type="button" class="vp-btn mic" class:active={call.micMuted} aria-pressed={call.micMuted}
				onclick={() => toggleMic()} aria-label={call.micMuted ? 'Mic muted' : 'Mute mic'} title={call.micMuted ? 'Unmute your microphone' : 'Mute your microphone'}>
				{#if call.micMuted}<MicOff size={18} />{:else}<Mic size={18} />{/if}
			</button>
			<button type="button" class="vp-btn danger" onclick={() => hangup()} aria-label="Stop voice agent" title="End call; background work continues">
				<PhoneOff size={18} />
			</button>
			<button type="button" class="vp-btn audio" class:active={call.agentMuted} aria-pressed={call.agentMuted}
				onclick={() => toggleAgent()} aria-label={call.agentMuted ? 'Agent muted' : 'Mute agent audio'} title={call.agentMuted ? 'Unmute the agent' : 'Mute the agent audio'}>
				{#if call.agentMuted}<VolumeX size={18} />{:else}<Volume2 size={18} />{/if}
			</button>
		</div>
	</div>
	<p class="vp-status" class:speaking={live && call.agentSpeaking && !micProblem} class:mic-problem={micProblem} role="status">{statusLabel}</p>
	{#if mine && (live || status === 'connecting')}<VoiceVolume />{/if}
	{#if realtime && live && workingLabel}<p class="vp-working">{workingLabel}</p>{/if}
	{#if realtime && costLabel}<span class="vp-cost" title="Estimated OpenAI spend for this call">{costLabel}</span>{/if}
</div>

<style>
	.voice-dock-wrapper { display: flex; flex-direction: column; align-items: center; flex-shrink: 0; gap: 4px; padding: 8px 12px; }
	.voice-dock-wrapper.compact { padding: 4px 0 8px; }
	.voice-ring { position: relative; width: 148px; height: 144px; isolation: isolate; }
	.ring-track, .ring-halo { position: absolute; inset: 5px 14px 19px; border-radius: 50%; pointer-events: none; }
	.ring-track { border: 1px solid var(--border); background: radial-gradient(circle, color-mix(in oklch, var(--primary) 9%, var(--background)), var(--background) 72%); }
	.live .ring-track { border-color: color-mix(in oklch, var(--primary) 42%, var(--border)); }
	.ring-halo { border: 2px solid transparent; opacity: 0; transition: opacity 160ms, transform 100ms; }
	.speaking .ring-halo { opacity: .8; border-color: var(--primary); box-shadow: 0 0 18px color-mix(in oklch, var(--primary) 22%, transparent); animation: breathe 1.5s ease-in-out infinite; }
	.hearing:not(.speaking) .ring-halo { opacity: .35; border-color: var(--primary); transform: scale(calc(1 + (var(--mic-scale) - 1) * .2)); }
	.ring-center { position: absolute; inset: 27px 35px 55px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 9px; }
	.waveform { display: flex; align-items: center; justify-content: center; gap: 4px; height: 25px; color: var(--muted-foreground); }
	.waveform i { width: 3px; height: 5px; border-radius: 6px; background: currentColor; }
	.speaking .waveform { color: var(--primary); }
	.speaking .waveform i { animation: wave 700ms ease-in-out infinite alternate; animation-delay: calc(var(--bar) * -130ms); }
	.hearing:not(.speaking) .waveform i { height: 10px; }
	.vp-elapsed { font: 10px var(--font-mono, monospace); color: var(--muted-foreground); font-variant-numeric: tabular-nums; }
	.vp-btn { position: absolute; display: grid; place-items: center; width: 44px; height: 44px; border: 1px solid var(--border); border-radius: 50%; background: var(--background); color: var(--foreground); cursor: pointer; box-shadow: 0 2px 8px #00000012; }
	.vp-btn.mic { left: 1px; bottom: 19px; }
	.vp-btn.audio { right: 1px; bottom: 19px; }
	.vp-btn.danger { left: 52px; bottom: 0; color: var(--destructive); background: color-mix(in oklch, var(--destructive) 10%, var(--background)); border-color: color-mix(in oklch, var(--destructive) 32%, var(--border)); }
	.vp-btn:hover { background: var(--muted); border-color: var(--primary); }
	.vp-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
	.vp-btn.active { color: var(--warning, #d97706); background: color-mix(in oklch, var(--warning, #d97706) 12%, var(--background)); }
	.vp-status, .vp-working { margin: 0; font-size: 11px; line-height: 1.4; color: var(--muted-foreground); }
	.vp-status.speaking { color: var(--primary); }
	.vp-status.mic-problem { color: var(--warning); }
	.vp-cost { font-size: 10px; color: var(--muted-foreground); }
	.vp-msg { margin: 0; max-width: 32rem; font-size: 12px; text-align: center; overflow-wrap: anywhere; }
	.err { color: var(--destructive); } .warn { color: var(--warning, #d97706); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	@keyframes breathe { 50% { transform: scale(1.045); opacity: .45; } }
	@keyframes wave { to { height: 23px; } }
	@media (prefers-reduced-motion: reduce) { .speaking .ring-halo, .speaking .waveform i { animation: none; } .speaking .waveform i { height: 16px; } .ring-halo { transition: none; } }
	@media (max-height: 600px) { .voice-dock-wrapper { padding-block: 0; } .voice-ring { height: 124px; } .ring-track, .ring-halo { inset: 4px 24px 20px; } .ring-center { top: 15px; } }
</style>
