<script lang="ts">
	/** An explicit microphone action on the current conversation. Legacy calls
	 * keep their original reconnect path; normal calls never create a new session. */
	import { onMount } from 'svelte';
	import { AudioLines } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { isVoiceEnabled } from '$lib/api/voice';
	import { startCallForSession, voiceCallState, isCallSurfaceActive } from '$lib/services/voiceCall';
	import { callBusyReason, explainBlockedCall, prefersTouch } from './call-gate';

	interface Props {
		disabled?: boolean;
		/** Why `disabled` is set (upload, recording, transcription), shown as the tooltip. */
		disabledReason?: string | null;
	}

	let { disabled = false, disabledReason = null }: Props = $props();

	const START_LABEL = 'Start a call in this conversation';

	let voiceEnabled = $state(false);
	let startingVoice = $state(false);

	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);
	const legacyVoice = $derived($activeSession.mode === 'voice');
	// A fresh composer has no conversation to attach a call to yet. The button
	// stays visible there (disabled) so the entry point is discoverable.
	const freshComposer = $derived(!$activeSession.sessionId);
	const permittedSurface = $derived(freshComposer || (!sessionRow?.hidden && !sessionRow?.parent_session_id && sessionRow?.status !== 'archived' && !sessionRow?.is_remote));
	const show = $derived(voiceEnabled && permittedSurface && !isCallSurfaceActive($voiceCallState, $activeSession.sessionId));
	// Calls start only on a ready or finished conversation (see callBusyReason).
	const blockedReason = $derived(
		freshComposer ? 'Send a message to start this conversation, then you can call it'
		: callBusyReason($activeSession, sessionRow)
			?? (disabled ? (disabledReason || 'Unavailable right now') : null)
	);
	// On a touch screen the tooltip never shows, so the blocked button stays
	// tappable (aria-disabled) and a tap says why.
	let touch = $state(false);

	onMount(async () => {
		touch = prefersTouch();
		voiceEnabled = await isVoiceEnabled();
	});

	/** Re-open a call on the voice session already being viewed. */
	async function resumeVoiceCall() {
		const sessionId = $activeSession.sessionId;
		if (blockedReason) {
			if (touch) explainBlockedCall(blockedReason);
			return;
		}
		if (startingVoice || !sessionId) return;
		startingVoice = true;
		try {
			await startCallForSession(sessionId, { attach: !legacyVoice });
		} finally {
			startingVoice = false;
		}
	}
</script>

{#if show}
	<button
		type="button"
		class="voice-call-btn"
		class:starting={startingVoice}
		onclick={resumeVoiceCall}
		class:blocked={!!blockedReason}
		disabled={(!!blockedReason && !touch) || startingVoice}
		aria-disabled={blockedReason ? 'true' : undefined}
		aria-label={START_LABEL}
		title={blockedReason ?? START_LABEL}
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
