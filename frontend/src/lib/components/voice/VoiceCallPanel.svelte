<script lang="ts">
	/**
	 * VoiceCallPanel — the call surface for a `mode='voice'` session (VW2).
	 *
	 * Mounted above MessageArea (same slot as TeamStatusBar) so the normal
	 * transcript still renders below it: the panel is the LIVE layer, the feed is
	 * the durable one (ruling VM9 — the backend writes the transcript, not us).
	 *
	 * The interaction contract (§1 of docs/VOICE_MODE_PLAN.md) is binding and
	 * says EXACTLY three controls while a call is live:
	 *   1. Stop voice agent — ends the call; the session stays.
	 *   2. Mute mic.
	 *   3. Mute agent audio.
	 * There is no push-to-talk and no per-utterance control: the channel is
	 * always open once live and turn-taking is server-side VAD. Do not add a
	 * fourth control here without re-opening that ruling.
	 *
	 * When the session is voice but no call is running, the panel collapses to a
	 * single "Start call" button, which re-mints a credential through the VW3
	 * route. While that route is unmerged the button reports it plainly instead
	 * of erroring.
	 */
	import { onDestroy } from 'svelte';
	import { Mic, MicOff, PhoneOff, Volume2, VolumeX, Phone } from 'lucide-svelte';
	import {
		voiceCallState,
		hangup,
		startCallForSession,
		toggleAgent,
		toggleMic,
	} from '$lib/services/voiceCall';
	import { getVoiceCallState } from '$lib/api/voice';

	interface Props {
		sessionId: string;
	}

	let { sessionId }: Props = $props();

	const call = $derived($voiceCallState);

	// The controller is a singleton: only reflect it when it is pointed at THIS
	// session, so switching to another voice session doesn't show a stale call.
	const mine = $derived(call.sessionId === sessionId);
	const status = $derived(mine ? call.status : 'idle');
	const live = $derived(status === 'live' || status === 'reconnecting');
	const connecting = $derived(status === 'connecting');

	const statusLabel = $derived(
		status === 'live'
			? 'Listening'
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
	// Spend estimate (VW5) — read from the server, which is the only side that
	// sees `response.done` usage. Deliberately slow (30s) and best-effort: this
	// is a glanceable "what is this costing me", not a meter.
	const COST_POLL_MS = 30_000;
	let costUsd = $state<number | null>(null);
	let costTimer: ReturnType<typeof setInterval> | null = null;

	async function pollCost() {
		try {
			const state = await getVoiceCallState(sessionId);
			costUsd = state?.estimated_cost_usd ?? null;
		} catch {
			/* a failed poll just leaves the last estimate on screen */
		}
	}

	$effect(() => {
		if (live) {
			void pollCost();
			costTimer ??= setInterval(() => void pollCost(), COST_POLL_MS);
		} else if (costTimer) {
			clearInterval(costTimer);
			costTimer = null;
		}
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

	// Orb scale follows the mic level; muted or idle sits still.
	const orbScale = $derived(
		live && !call.micMuted ? 1 + Math.min(call.level, 1) * 0.55 : 1
	);

	let starting = $state(false);
	async function onStart() {
		if (starting) return;
		starting = true;
		try {
			await startCallForSession(sessionId);
		} finally {
			starting = false;
		}
	}
</script>

<div class="voice-panel" class:live>
	<div class="vp-head">
		<div class="orb-wrap" aria-hidden="true">
			<span class="orb-pulse" style={`transform: scale(${orbScale});`}></span>
			<span class="orb" class:on={live} class:muted={call.micMuted}></span>
		</div>
		<div class="vp-title">
			<h3>Voice agent</h3>
			<p>
				{statusLabel}
				{#if elapsed}<span class="vp-elapsed">· {elapsed}</span>{/if}
				{#if mine && call.model}<span class="vp-model">· {call.model}</span>{/if}
				{#if mine && costLabel}<span
						class="vp-cost"
						title="Estimated OpenAI spend for this call (list prices, cached-input discounts ignored)"
						>· {costLabel}</span
					>{/if}
			</p>
		</div>

		<div class="vp-controls">
			{#if live || connecting}
				<!-- Contract control 1/3 — ends the call, keeps the session. -->
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
					{#if call.micMuted}<MicOff size={15} strokeWidth={1.7} />{:else}<Mic size={15} strokeWidth={1.7} />{/if}
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
					{#if call.agentMuted}<VolumeX size={15} strokeWidth={1.7} />{:else}<Volume2 size={15} strokeWidth={1.7} />{/if}
					<span>{call.agentMuted ? 'Agent muted' : 'Mute agent audio'}</span>
				</button>
			{:else}
				<button type="button" class="vp-btn primary" onclick={onStart} disabled={starting}>
					<Phone size={15} strokeWidth={1.7} />
					<span>{starting ? 'Starting…' : 'Start call'}</span>
				</button>
			{/if}
		</div>
	</div>

	{#if mine && call.error}
		<p class="vp-msg err" role="alert">{call.error}</p>
	{/if}
	{#if mine && call.notice}
		<p class="vp-msg warn">{call.notice}</p>
	{/if}

	{#if live}
		<div class="vp-captions" aria-live="polite" aria-label="Live captions">
			{#if call.captions.length === 0}
				<p class="vp-hint">Just start talking — the channel stays open.</p>
			{:else}
				{#each call.captions as caption (caption.role + caption.id)}
					<p class="cap" class:assistant={caption.role === 'assistant'} class:pending={!caption.final}>
						<span class="cap-who">{caption.role === 'assistant' ? 'Agent' : 'You'}</span>
						<span class="cap-text">{caption.text}</span>
					</p>
				{/each}
			{/if}
			<p class="vp-foot">Captions are live only — the saved transcript appears in the conversation below.</p>
		</div>
	{/if}
</div>

<style>
	.voice-panel {
		flex-shrink: 0;
		margin: 8px 12px 4px;
		padding: 12px 14px;
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		background: color-mix(in oklch, var(--primary) 5%, var(--background));
	}
	.voice-panel.live {
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
	.voice-panel:not(.live) .orb-pulse {
		opacity: 0;
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
	.vp-elapsed,
	.vp-model,
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
	.vp-btn.primary {
		color: var(--primary-foreground, #fff);
		background: var(--primary);
		border-color: var(--primary);
	}

	.vp-msg {
		margin: 8px 0 0;
		font-size: 12.5px;
	}
	.vp-msg.err {
		color: var(--destructive);
	}
	.vp-msg.warn {
		color: var(--warning, #d97706);
	}

	.vp-captions {
		margin-top: 10px;
		padding-top: 8px;
		border-top: 1px solid var(--border);
		max-height: 180px;
		overflow-y: auto;
	}
	.cap {
		margin: 0 0 4px;
		font-size: 12.5px;
		line-height: 1.5;
		color: var(--foreground);
	}
	.cap.assistant .cap-who {
		color: var(--primary);
	}
	.cap.pending {
		opacity: 0.75;
	}
	.cap-who {
		display: inline-block;
		min-width: 44px;
		font-size: 11px;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.vp-hint,
	.vp-foot {
		margin: 0;
		font-size: 11.5px;
		color: var(--muted-foreground);
	}
	.vp-foot {
		margin-top: 6px;
		opacity: 0.75;
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
