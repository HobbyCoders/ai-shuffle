<script lang="ts">
	/** Session-scoped call status and controls. Compact mode sits beside the
	 * persistent text composer; unmounting this view never ends the call. */
	import { onDestroy, untrack } from 'svelte';
	import { Mic, MicOff, PhoneOff, Volume2, VolumeX } from 'lucide-svelte';
	import { voiceCallState, hangup, toggleAgent, toggleMic } from '$lib/services/voiceCall';
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
	const modeLabel = $derived(delegated ? 'Selected-model delegation' : call.model === 'gpt-live-1' ? 'OpenAI Responses delegation' : realtime ? (call.options?.turn_detection === 'server_vad' ? 'Realtime agent tools · Normal VAD' : 'Realtime agent tools') : 'Full-duplex conversation');

	const statusLabel = $derived(
		status === 'live'
			? call.micMuted
				? 'Mic muted'
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

<!-- Composer slot (VM18): same outer metrics as .chat-input-wrapper /
     .chat-input-container in ChatInput, so the swap happens in place without the
     chat column shifting. -->
<div class="voice-dock-wrapper" class:compact data-testid="voice-call-dock">
	<div class="voice-dock" class:live>
		{#if mine && call.error}
			<p class="vp-msg err" role="alert">{call.error}</p>
		{/if}
		{#if mine && call.notice}
			<p class="vp-msg warn">{call.notice}</p>
		{/if}

		<div class="vp-head">
			<div class="orb-wrap" aria-hidden="true">
				<span
					class="orb-pulse"
					class:speaking={live && call.agentSpeaking}
					style={live && call.agentSpeaking ? '' : `transform: scale(${orbScale});`}
				></span>
				<span class="orb" class:on={live} class:muted={call.micMuted} class:speaking={live && call.agentSpeaking}></span>
			</div>
			<div class="vp-title">
				<h3>{engineLabel}</h3>
				{#if mine && call.model}<p class="vp-mode">{modeLabel}</p>{/if}
				<p>
					<span class="vp-status" class:speaking={live && call.agentSpeaking}>{statusLabel}</span>
					{#if elapsed}<span class="vp-elapsed">· {elapsed}</span>{/if}
					{#if realtime && live && workingLabel}<span class="vp-working">· {workingLabel}</span>{/if}
					{#if realtime && costLabel}<span
							class="vp-cost"
							title="Estimated OpenAI spend for this call (list prices, cached-input discounts ignored)"
							>· {costLabel}</span
						>{/if}
				</p>
			</div>

			<div class="vp-controls">
				<!-- Contract control 1/3 — ends the call, keeps the session. The
				     composer returns the moment this lands. -->
				<button
					type="button"
					class="vp-btn danger"
					onclick={() => hangup()}
					title="Stop the voice agent (the session and any dispatched agents stay)"
				>
					<PhoneOff size={15} strokeWidth={1.7} />
					<span>Stop voice agent</span>
				</button>
				<!-- Contract control 2/3 -->
				<button
					type="button"
					class="vp-btn"
					class:active={call.micMuted}
					aria-pressed={call.micMuted}
					onclick={() => toggleMic()}
					title={call.micMuted ? 'Unmute your microphone' : 'Mute your microphone'}
				>
					{#if call.micMuted}<MicOff size={15} strokeWidth={1.7} />{:else}<Mic
							size={15}
							strokeWidth={1.7}
						/>{/if}
					<span>{call.micMuted ? 'Mic muted' : 'Mute mic'}</span>
				</button>
				<!-- Contract control 3/3 -->
				<button
					type="button"
					class="vp-btn"
					class:active={call.agentMuted}
					aria-pressed={call.agentMuted}
					onclick={() => toggleAgent()}
					title={call.agentMuted ? 'Unmute the agent' : 'Mute the agent audio'}
				>
					{#if call.agentMuted}<VolumeX size={15} strokeWidth={1.7} />{:else}<Volume2
							size={15}
							strokeWidth={1.7}
						/>{/if}
					<span>{call.agentMuted ? 'Agent muted' : 'Mute agent audio'}</span>
				</button>
			</div>
		</div>
	</div>
</div>

<style>
	.compact.voice-dock-wrapper { width: 100%; padding: 0; margin: 0 0 8px; }
	.compact .voice-dock { padding: 8px 12px; }
	.compact .orb-wrap { display: none; }
	.compact .vp-head { gap: 8px; }
	.compact .vp-title h3 { font-size: 12px; }

	/* Mirrors .chat-input-wrapper / .chat-input-container so the dock occupies the
	   composer's exact footprint (VM18). */
	.voice-dock-wrapper {
		padding: 0.5rem 0.75rem 0.75rem;
		flex-shrink: 0;
	}

	@media (min-width: 640px) {
		.voice-dock-wrapper {
			padding: 0.75rem 1rem 1rem;
		}
	}

	.voice-dock {
		max-width: 56rem;
		margin: 0 auto;
		padding: 10px 12px;
		border: 1px solid var(--glass-border, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 1.33);
		background: color-mix(in oklch, var(--primary) 5%, var(--background));
	}
	.voice-dock.live {
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
	}

	.vp-head {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	/* Status orb — the pulse ring is driven by the mic RMS level. */
	.orb-wrap {
		position: relative;
		width: 34px;
		height: 34px;
		flex-shrink: 0;
		display: grid;
		place-items: center;
	}
	.orb {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--muted-foreground);
		opacity: 0.5;
	}
	.orb.on {
		background: var(--primary);
		opacity: 1;
	}
	.orb.muted {
		background: var(--warning, #d97706);
	}
	.orb-pulse {
		position: absolute;
		inset: 10px;
		border-radius: 50%;
		background: color-mix(in oklch, var(--primary) 30%, transparent);
		transition: transform 90ms linear;
	}
	.orb-pulse.speaking {
		animation: orb-breathe 1.1s ease-in-out infinite;
	}
	.orb.speaking {
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 22%, transparent);
	}
	@keyframes orb-breathe {
		0%,
		100% {
			transform: scale(1.05);
		}
		50% {
			transform: scale(1.5);
		}
	}
	.voice-dock:not(.live) .orb-pulse {
		opacity: 0;
	}
	@media (prefers-reduced-motion: reduce) {
		.orb-pulse.speaking {
			animation: none;
			transform: scale(1.3);
		}
	}

	.vp-title {
		flex: 1;
		min-width: 0;
	}
	.vp-title h3 {
		margin: 0;
		font-size: 13.5px;
		font-weight: 600;
		color: var(--foreground);
	}
	.vp-title p {
		margin: 2px 0 0;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.vp-status.speaking {
		color: var(--primary);
	}
	.vp-working {
		color: var(--foreground);
	}
	.vp-elapsed,
	.vp-cost {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
	}
	.vp-cost {
		cursor: help;
	}

	.vp-controls {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.vp-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 30px;
		padding: 0 11px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--background);
		color: var(--foreground);
		font-size: 12px;
		font-weight: 500;
		cursor: pointer;
	}
	.vp-btn:hover:not(:disabled) {
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
	}
	.vp-btn:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}
	.vp-btn.active {
		color: var(--warning, #d97706);
		border-color: color-mix(in oklch, var(--warning, #d97706) 45%, var(--border));
		background: color-mix(in oklch, var(--warning, #d97706) 12%, transparent);
	}
	.vp-btn.danger {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 40%, var(--border));
	}

	.vp-msg {
		margin: 0 0 8px;
		font-size: 12.5px;
	}
	.vp-msg.err {
		color: var(--destructive);
	}
	.vp-msg.warn {
		color: var(--warning, #d97706);
	}

	@media (max-width: 700px) {
		.vp-btn span {
			display: none;
		}
		.vp-btn {
			padding: 0 9px;
		}
	}
</style>
