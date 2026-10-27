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
	import { reportCallStartFailure } from '$lib/services/voiceCallOutcome';
	import { callBusyReason, explainBlockedCall, freshCallReason, prefersTouch } from './call-gate';

	interface Props {
		disabled?: boolean;
		/** Why `disabled` is set (upload, recording, transcription), shown as the tooltip. */
		disabledReason?: string | null;
		/**
		 * `circle`: the composer's Talk circle beside Send (the waveform in the
		 * accent on a light accent tint). It holds the send slot while there is
		 * nothing to send. Same gate and the same call path as the icon.
		 */
		variant?: 'icon' | 'circle';
		/** The agent's name: a new session's button reads "Talk to <agent>". */
		agentName?: string | null;
		/**
		 * Out: whether the button shows at all (voice on, a surface that takes
		 * calls, no call already running here). The composer shows Send in its
		 * place when it does not.
		 */
		available?: boolean;
	}

	let { disabled = false, disabledReason = null, variant = 'icon', agentName = null, available = $bindable(false) }: Props = $props();

	let voiceEnabled = $state(false);
	let startingVoice = $state(false);

	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);
	const legacyVoice = $derived($activeSession.mode === 'voice');
	// The new-session composer has no conversation yet; the call creates one.
	const freshComposer = $derived(!$activeSession.sessionId);
	// A conversation another worker runs takes a call too: the call runs here
	// and forwards to that worker (#700).
	const permittedSurface = $derived(freshComposer || (!sessionRow?.hidden && !sessionRow?.parent_session_id && sessionRow?.status !== 'archived'));
	const show = $derived(voiceEnabled && permittedSurface && !isCallSurfaceActive($voiceCallState, $activeSession.sessionId));
	// Every conversation takes a call at any time, even while it works (see
	// callBusyReason); only one whose worker is offline is refused.
	const blockedReason = $derived(
		(freshComposer ? freshCallReason($activeSession) : null)
			?? callBusyReason($activeSession, sessionRow)
			?? (disabled ? (disabledReason || 'Unavailable right now') : null)
	);
	const label = $derived(
		freshComposer
			? (agentName?.trim() ? `Talk to ${agentName.trim()}` : 'Start a call in a new conversation')
			: 'Start a call in this conversation'
	);
	$effect(() => {
		available = show;
	});
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
		let sessionId = $activeSession.sessionId;
		try {
			if (!sessionId) {
				// Settle a call running elsewhere before anything is created, so
				// declining leaves no new session behind.
				if (!(await confirmCallReplacement(null))) return;
				// Errors surface where a failed first send shows them.
				sessionId = await activeSession.startSessionWithoutPrompt();
				if (!sessionId) return;
			}
			await startCallForSession(sessionId, { attach: !legacyVoice });
		} catch (e) {
			// A start the call controller refused outright (another call came up
			// meanwhile) would otherwise be an unhandled rejection with no reason
			// on screen.
			console.error('[voice] call start failed:', e);
			reportCallStartFailure(e, sessionId);
		} finally {
			startingVoice = false;
		}
	}
</script>

{#if show && variant === 'circle'}
	<button
		type="button"
		class="call-circle"
		class:starting={startingVoice}
		class:blocked={!!blockedReason}
		onclick={startVoiceCall}
		disabled={(!!blockedReason && !touch) || startingVoice}
		aria-disabled={blockedReason ? 'true' : undefined}
		aria-busy={startingVoice || undefined}
		aria-label={label}
		title={blockedReason ?? label}
	>
		<AudioLines size={17} strokeWidth={1.9} aria-hidden="true" />
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

	/* Circle variant: the call button in the composer's button row: the
	   waveform in the accent on a light accent tint. Its corners follow the
	   composer's (--composer-btn-radius, set by ChatInput), so it is square
	   on a square theme and round on the rest. */
	.call-circle {
		display: grid;
		place-items: center;
		flex: none;
		width: 32px;
		height: 32px;
		padding: 0;
		border: none;
		border-radius: var(--composer-btn-radius, 50%);
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 22%, transparent);
		color: var(--primary);
		cursor: pointer;
		transition: background-color 150ms ease, transform 100ms ease;
	}
	.call-circle:hover:not(:disabled):not(.blocked) {
		background: color-mix(in oklch, var(--primary) 17%, transparent);
	}
	.call-circle:active:not(:disabled):not(.blocked) { transform: scale(0.94); }
	.call-circle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.call-circle:disabled, .call-circle.blocked { opacity: 0.4; cursor: not-allowed; }
	.call-circle.starting { cursor: progress; }
	.call-circle.starting :global(svg) { animation: voice-call-pulse 1s ease-in-out infinite; }
	@media (prefers-reduced-motion: reduce) {
		.call-circle { transition: none; }
		.call-circle.starting :global(svg) { animation: none; }
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
