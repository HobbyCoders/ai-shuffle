<script lang="ts">
	/**
	 * VoiceCallButton — the "start a voice call" action button (ruling VM19).
	 *
	 * VM19 (user directive, 2026-08-15): voice activation is an ACTION BUTTON in
	 * the composer's send-button cluster — next to the VoiceRecorder (STT) button
	 * and the send button — not a pill in the ContextPillBar. Same gating as the
	 * pill it replaces (VM6/VM18): shown when voice mode is enabled AND the view
	 * is either a brand-new chat (create a voice session and call it) or an
	 * existing voice session with no live call (re-open the call). This button
	 * lives inside ChatInput, which a live call replaces wholesale (VM18), so it
	 * is never on screen during a call — no extra live-call gating needed.
	 */
	import { onMount } from 'svelte';
	import { AudioLines } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { appData, sessions } from '$lib/stores/app-data.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { notify } from '$lib/stores/notifications';
	import { createVoiceSession, isVoiceEnabled } from '$lib/api/voice';
	import { startCall, startCallForSession } from '$lib/services/voiceCall';

	interface Props {
		disabled?: boolean;
	}

	let { disabled = false }: Props = $props();

	let voiceEnabled = $state(false);
	let startingVoice = $state(false);

	const sessionRow = $derived(
		$sessions.find((s) => s.id === $activeSession.sessionId) ?? null
	);
	const isEmptyState = $derived($activeSession.messages.length === 0);
	const isVoiceSession = $derived(sessionRow?.mode === 'voice');
	const show = $derived(voiceEnabled && (isEmptyState || isVoiceSession));

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

	async function startVoiceSession() {
		if (startingVoice) return;
		startingVoice = true;
		try {
			const created = await createVoiceSession({
				profile_id: $activeSession.profile || null,
				project_id: $activeSession.project || null
			});
			// The board row must exist before we switch: ChatView reads `mode`
			// off the sessions list to decide the surface.
			await appData.loadSessions();
			activeSession.switchSession(
				created.session_id,
				$activeSession.project || null,
				null,
				'Voice session'
			);
			layout.openChat(created.session_id);
			// Auto-start — the affordance IS the call (the channel is always open
			// once live; there is no separate "connect" step for the user).
			await startCall(created.session_id, {
				client_secret: created.client_secret,
				model: created.model
			});
		} catch (e) {
			// The backend's 400/403 detail strings are written for the user
			// (missing/ineligible provider, voice mode disabled) — show them as-is.
			const detail = (e as { detail?: string } | null)?.detail;
			notify.error(detail || 'Could not start a voice session.');
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
		onclick={isVoiceSession ? resumeVoiceCall : startVoiceSession}
		disabled={disabled || startingVoice}
		aria-label={isVoiceSession ? 'Start a call on this voice session' : 'Start a voice call'}
		title={isVoiceSession
			? 'Start a call on this voice session'
			: 'Start a voice call — talk to an agent that dispatches the work'}
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
