<script lang="ts">
	/** An explicit microphone action on the current conversation. Legacy calls
	 * keep their original reconnect path. On the new-session composer the
	 * press creates a normal conversation from the composer's picks first. */
	import { onMount } from 'svelte';
	import { AudioLines } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { isVoiceEnabled } from '$lib/api/voice';
	import { confirmCallReplacement, startCallForSession, voiceCallState, isCallSurfaceActive } from '$lib/services/voiceCall';
	import { callBusyReason, explainBlockedCall, freshCallReason, prefersTouch } from './call-gate';

	interface Props {
		disabled?: boolean;
		/** Why `disabled` is set (upload, recording, transcription), shown as the tooltip. */
		disabledReason?: string | null;
		/**
		 * `card`: the new-session agent card's "Talk to <agent>" button (#371
		 * step 4): a round orb in the agent's colour with words beside it. Same
		 * gate and the same call path as the dock's icon.
		 */
		variant?: 'icon' | 'card';
		/** The agent's name, for the card label. */
		agentName?: string | null;
	}

	let { disabled = false, disabledReason = null, variant = 'icon', agentName = null }: Props = $props();

	let voiceEnabled = $state(false);
	let startingVoice = $state(false);

	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);
	const legacyVoice = $derived($activeSession.mode === 'voice');
	// The new-session composer has no conversation yet; the call creates one.
	const freshComposer = $derived(!$activeSession.sessionId);
	const permittedSurface = $derived(freshComposer || (!sessionRow?.hidden && !sessionRow?.parent_session_id && sessionRow?.status !== 'archived' && !sessionRow?.is_remote));
	const show = $derived(voiceEnabled && permittedSurface && !isCallSurfaceActive($voiceCallState, $activeSession.sessionId));
	// Every conversation takes a call at any time, even while it works (see
	// callBusyReason); only one another device runs is refused.
	const blockedReason = $derived(
		(freshComposer ? freshCallReason($activeSession) : null)
			?? callBusyReason($activeSession, sessionRow)
			?? (disabled ? (disabledReason || 'Unavailable right now') : null)
	);
	const label = $derived(freshComposer ? 'Start a call in a new conversation' : 'Start a call in this conversation');
	const cardLabel = $derived(`Talk to ${agentName?.trim() || 'your agent'}`);
	// On a touch screen the tooltip never shows, so the blocked button stays
	// tappable (aria-disabled) and a tap says why.
	let touch = $state(false);

	onMount(async () => {
		touch = prefersTouch();
		voiceEnabled = await isVoiceEnabled();
	});

	async function startVoiceCall() {
		if (blockedReason) {
			if (touch) explainBlockedCall(blockedReason);
			return;
		}
		if (startingVoice) return;
		startingVoice = true;
		try {
			let sessionId = $activeSession.sessionId;
			if (!sessionId) {
				// Settle a call running elsewhere before anything is created, so
				// declining leaves no new session behind.
				if (!(await confirmCallReplacement(null))) return;
				// Errors surface where a failed first send shows them.
				sessionId = await activeSession.startSessionWithoutPrompt();
				if (!sessionId) return;
			}
			await startCallForSession(sessionId, { attach: !legacyVoice });
		} finally {
			startingVoice = false;
		}
	}
</script>

{#if show && variant === 'card'}
	<button
		type="button"
		class="call-card"
		class:starting={startingVoice}
		class:blocked={!!blockedReason}
		onclick={startVoiceCall}
		disabled={(!!blockedReason && !touch) || startingVoice}
		aria-disabled={blockedReason ? 'true' : undefined}
		aria-label={cardLabel}
		title={blockedReason ?? `${cardLabel}: ${label.toLowerCase()}`}
	>
		<span class="call-card-label">{cardLabel}</span>
		<span class="call-orb" aria-hidden="true"><AudioLines size={18} strokeWidth={1.8} /></span>
	</button>
{:else if show}
	<button
		type="button"
		class="voice-call-btn"
		class:starting={startingVoice}
		onclick={startVoiceCall}
		class:blocked={!!blockedReason}
		disabled={(!!blockedReason && !touch) || startingVoice}
		aria-disabled={blockedReason ? 'true' : undefined}
		aria-label={label}
		title={blockedReason ?? label}
	>
		<AudioLines size={15} strokeWidth={1.7} />
	</button>
{/if}

<style>
	/* Same footprint as the VoiceRecorder's .voice-btn so the three actions
	   (call, STT, send) read as one cluster — primary accent so it reads as a
	   mode choice rather than another input control. */
	.voice-call-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: min(50%, calc(var(--radius, 0.75rem) * 2.67));
		cursor: pointer;
		color: var(--primary);
		transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
	}

	.voice-call-btn:hover:not(:disabled):not(.blocked) {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
	}

	.voice-call-btn:active:not(:disabled):not(.blocked) {
		transform: scale(0.95);
	}

	.voice-call-btn:disabled,
	.voice-call-btn.blocked {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.voice-call-btn.starting :global(svg) {
		animation: voice-call-pulse 1s ease-in-out infinite;
	}

	.voice-call-btn:focus-visible { outline:2px solid var(--ring); outline-offset:3px; }

	/* Card variant: words + a round orb tinted by the agent card's --agent
	   (an avatar colour token, falling back to the primary accent). */
	.call-card {
		--orb: var(--agent, var(--primary));
		display: inline-flex;
		align-items: center;
		gap: 10px;
		height: 48px;
		padding: 0 2px 0 14px;
		border: none;
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 13px;
		font-weight: 550;
		white-space: nowrap;
		cursor: pointer;
		transition: color 150ms cubic-bezier(0.4, 0, 0.2, 1);
	}
	.call-orb {
		width: 44px;
		height: 44px;
		border-radius: 50%;
		display: grid;
		place-items: center;
		color: var(--orb);
		background: color-mix(in oklch, var(--orb) 14%, var(--card));
		border: 1px solid color-mix(in oklch, var(--orb) 45%, var(--border));
		box-shadow: 0 0 0 4px color-mix(in oklch, var(--orb) 7%, transparent);
		transition: background 200ms ease, transform 200ms cubic-bezier(0.16, 1, 0.3, 1), box-shadow 200ms ease;
	}
	.call-card:hover:not(:disabled):not(.blocked) { color: var(--foreground); }
	.call-card:hover:not(:disabled):not(.blocked) .call-orb {
		background: color-mix(in oklch, var(--orb) 24%, var(--card));
		box-shadow: 0 0 0 6px color-mix(in oklch, var(--orb) 10%, transparent);
		transform: scale(1.04);
	}
	.call-card:active:not(:disabled):not(.blocked) .call-orb { transform: scale(0.96); }
	.call-card:focus-visible { outline: none; }
	.call-card:focus-visible .call-orb { outline: 2px solid var(--ring); outline-offset: 3px; }
	.call-card:disabled, .call-card.blocked { cursor: not-allowed; }
	.call-card:disabled .call-orb, .call-card.blocked .call-orb { opacity: 0.4; box-shadow: none; }
	.call-card.starting .call-orb :global(svg) { animation: voice-call-pulse 1s ease-in-out infinite; }
	@media (prefers-reduced-motion: reduce) {
		.call-card, .call-orb { transition: none; }
		.call-card.starting .call-orb :global(svg) { animation: none; }
	}
	@media (hover:none) { .voice-call-btn { min-width:44px; min-height:44px; } }
	@media (prefers-reduced-motion:reduce) { .voice-call-btn { transition:none; } .voice-call-btn.starting :global(svg) { animation:none; } }
	@keyframes voice-call-pulse {
		0%,
		100% {
			transform: scale(1);
			opacity: 0.7;
		}
		50% {
			transform: scale(1.1);
			opacity: 1;
		}
	}
</style>
