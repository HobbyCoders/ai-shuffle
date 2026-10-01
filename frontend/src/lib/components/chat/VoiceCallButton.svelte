<script lang="ts">
	/** An explicit microphone action on the current conversation. Legacy calls
	 * keep their original reconnect path; normal calls never create a new session. */
	import { onMount } from 'svelte';
	import { AudioLines } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { isVoiceEnabled } from '$lib/api/voice';
	import { startCallForSession, voiceCallState, isCallSurfaceActive } from '$lib/services/voiceCall';
	import { hasStoppableWork } from './stoppable-work';

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
	// Calls start only on a ready or finished conversation. Busy is the Stop
	// button's signal (streaming, running tasks, has_running_tasks, remote
	// runtime) plus the sidebar's Parked marker and an unanswered prompt.
	const working = $derived(hasStoppableWork($activeSession, sessionRow ?? undefined)
		|| $activeSession.pendingPermissions.length > 0
		|| $activeSession.pendingQuestions.length > 0);
	const parked = $derived(!!sessionRow?.parked_task_kind);
	const blockedReason = $derived(
		freshComposer ? 'Send a message to start this conversation, then you can call it'
		: working ? 'Calls can start once this session finishes working'
		: parked ? 'Calls can start once this session’s background tasks finish'
		: disabled ? (disabledReason || 'Unavailable right now')
		: null
	);

	onMount(async () => {
		voiceEnabled = await isVoiceEnabled();
	});

	/** Re-open a call on the voice session already being viewed. */
	async function resumeVoiceCall() {
		const sessionId = $activeSession.sessionId;
		if (startingVoice || !sessionId || blockedReason) return;
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
		disabled={!!blockedReason || startingVoice}
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

	.voice-call-btn:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
	}

	.voice-call-btn:active:not(:disabled) {
		transform: scale(0.95);
	}

	.voice-call-btn:disabled {
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
