<script lang="ts">
	/**
	 * VoiceCallButton — re-open the call on the voice session being viewed
	 * (ruling VM19).
	 *
	 * VM19 (user directive, 2026-08-15): voice activation is an ACTION BUTTON in
	 * the composer's controls cluster — next to the VoiceRecorder (STT) button
	 * and the send button — not a pill in the ContextPillBar. It originally
	 * doubled as "create a voice session" from any brand-new chat (VM6/VM18
	 * gating), because a call had nowhere else to start from. Voice is now its
	 * own sidepanel lane with its own new-session screen, so creation moved to
	 * `startNewVoiceSession` and this button keeps only the resume half: it
	 * shows on an existing voice session and nowhere else. A live call replaces
	 * ChatInput wholesale (VM18), so the button is never on screen during one —
	 * no extra live-call gating needed.
	 */
	import { onMount } from 'svelte';
	import { AudioLines } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { isVoiceEnabled } from '$lib/api/voice';
	import { startCallForSession, voiceCallState, isCallSurfaceActive } from '$lib/services/voiceCall';

	interface Props {
		disabled?: boolean;
	}

	let { disabled = false }: Props = $props();

	let voiceEnabled = $state(false);
	let startingVoice = $state(false);

	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);
	const isVoiceSession = $derived(sessionRow?.mode === 'voice');
	const show = $derived(voiceEnabled && isVoiceSession && !isCallSurfaceActive($voiceCallState, $activeSession.sessionId));

	onMount(async () => {
		voiceEnabled = await isVoiceEnabled();
	});

	/** Re-open a call on the voice session already being viewed. */
	async function resumeVoiceCall() {
		const sessionId = $activeSession.sessionId;
		if (startingVoice || !sessionId) return;
		startingVoice = true;
		try {
			await startCallForSession(sessionId);
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
		disabled={disabled || startingVoice}
		aria-label="Start a call on this voice session"
		title="Start a call on this voice session"
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
