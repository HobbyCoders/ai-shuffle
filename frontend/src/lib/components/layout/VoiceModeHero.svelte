<script lang="ts">
	/**
	 * VoiceModeHero — the new-session screen for the VOICE lane.
	 *
	 * The voice lane offers exactly one thing on a fresh surface: start a call.
	 * There is no composer here (ChatView withholds it for this case), so this
	 * button is the ONLY action on screen — which is why it is styled as a real
	 * primary and why its pending state matters. Minting the session and
	 * negotiating WebRTC takes a visible beat; a button that looks inert for a
	 * second reads as a dead click and invites a second one.
	 *
	 * Gating on `isVoiceEnabled()` is not decoration: with voice mode off in
	 * settings the session mint fails, so rendering a button that can only error
	 * would be a trap. The settings link mirrors WorkspaceSelector's "No agents
	 * yet" affordance — name what is missing where the user is already looking,
	 * and take them to the place that fixes it.
	 */
	import { onMount } from 'svelte';
	import { AudioLines, Loader2 } from 'lucide-svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { isVoiceEnabled } from '$lib/api/voice';
	import { startNewVoiceSession } from '$lib/services/startVoiceSession';

	// `null` while the (module-cached) settings read is in flight. The action
	// area holds its space rather than flashing the disabled notice first.
	let voiceEnabled = $state<boolean | null>(null);
	let starting = $state(false);

	onMount(async () => {
		voiceEnabled = await isVoiceEnabled();
	});

	async function startCall() {
		if (starting) return;
		starting = true;
		try {
			// Errors are already surfaced as a toast by the service; a false return
			// just means we stay on this hero so the user can retry.
			await startNewVoiceSession();
		} finally {
			starting = false;
		}
	}
</script>

<div class="voice-mode-hero">
	<div class="hero-mark" class:pending={starting} aria-hidden="true">
		<AudioLines size={22} strokeWidth={1.5} />
	</div>

	<h1 class="hero-heading">Talk it through</h1>

	<p class="hero-subline">
		A spoken conversation with the voice agent. It can dispatch work to your agents while you talk,
		and the transcript lands in this session as you go.
	</p>

	<div class="hero-action">
		{#if voiceEnabled === false}
			<p class="hero-disabled">Voice mode is turned off.</p>
			<button type="button" class="hero-link" onclick={() => layout.openFeaturePage('settings')}>
				Enable it in Settings
			</button>
		{:else if voiceEnabled}
			<button
				type="button"
				class="start-call"
				onclick={startCall}
				disabled={starting}
				aria-busy={starting}
			>
				{#if starting}
					<Loader2 size={18} strokeWidth={2} class="spin" />
					<span>Connecting…</span>
				{:else}
					<AudioLines size={18} strokeWidth={2} />
					<span>Start a voice call</span>
				{/if}
			</button>
		{/if}
	</div>
</div>

<style>
	/* Same centered column and metrics as WorkspaceSelector / ChatModeHero. */
	.voice-mode-hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 2rem 2rem 1.25rem;
		text-align: center;
		user-select: none;
	}

	.hero-mark {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		margin-bottom: 10px;
		border-radius: 50%;
		color: var(--subtle-foreground);
		background: var(--scrim);
		border: 1px solid var(--border);
		transition: color 200ms ease, border-color 200ms ease;
	}

	/* The mark picks up the accent while the call is being set up, so the whole
	   hero — not just the button — reads as "working". */
	.hero-mark.pending {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
	}

	.hero-heading {
		font-family: var(--font-display, Georgia, serif);
		font-size: clamp(1.375rem, 4vw, 2rem);
		font-weight: 700;
		color: var(--foreground);
		letter-spacing: -0.02em;
		line-height: 1.15;
	}

	.hero-subline {
		font-size: 0.875rem;
		color: var(--muted-foreground);
		line-height: 1.5;
		max-width: 42ch;
	}

	/* Reserves the action row's height while the settings read resolves, so the
	   hero doesn't jump once we know whether voice is on. */
	.hero-action {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		min-height: 42px;
		margin-top: 14px;
	}

	.start-call {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		height: 42px;
		padding: 0 20px;
		border: 1px solid transparent;
		border-radius: 999px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 0.9375rem;
		font-weight: 600;
		cursor: pointer;
		box-shadow: var(--shadow-s);
		transition: filter 150ms ease, box-shadow 150ms ease;
	}

	.start-call:hover:not(:disabled) {
		filter: brightness(1.06);
		box-shadow: var(--shadow-m);
	}

	.start-call:disabled {
		cursor: progress;
		opacity: 0.8;
	}

	.start-call :global(.spin) {
		animation: startCallSpin 900ms linear infinite;
	}

	@keyframes startCallSpin {
		to {
			transform: rotate(360deg);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.start-call :global(.spin) {
			animation: none;
		}
	}

	.hero-disabled {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	/* Matches WorkspaceSelector's .no-agents link affordance. */
	.hero-link {
		background: none;
		border: none;
		padding: 0;
		color: var(--primary);
		cursor: pointer;
		font-size: 0.8125rem;
	}

	.hero-link:hover {
		text-decoration: underline;
	}
</style>
