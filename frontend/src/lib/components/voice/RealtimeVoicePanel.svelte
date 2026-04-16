<script lang="ts">
	/**
	 * RealtimeVoicePanel — drop-in voice call UI driven by Pipecat pipeline.
	 *
	 * State machine: idle → connecting → connected → listening → thinking
	 *                → speaking → (loop) → disconnected | error
	 *
	 * Emits events so the parent can persist transcripts / switch views.
	 * Mobile-aware: prefers push-to-talk on touch devices; VAD on desktop.
	 */
	import { onDestroy } from 'svelte';
	import {
		RealtimeVoiceSession,
		type VoiceState
	} from '$lib/services/realtimeVoice';

	type Role = 'user' | 'assistant';

	interface Props {
		projectId?: string;
		sessionId?: string;
		systemPrompt?: string;
		/** Preferred input mode on first load — user can toggle. */
		defaultMode?: 'vad' | 'push-to-talk';
		onTranscript?: (text: string, role: Role) => void;
		onStateChange?: (state: VoiceState) => void;
		onError?: (error: Error) => void;
	}

	let {
		projectId,
		sessionId,
		systemPrompt,
		defaultMode,
		onTranscript,
		onStateChange,
		onError
	}: Props = $props();

	let session: RealtimeVoiceSession | null = null;
	let state = $state<VoiceState>('idle');
	let errorMsg = $state('');
	let muted = $state(false);
	let mode = $state<'vad' | 'push-to-talk'>(defaultMode ?? detectDefaultMode());
	let transcripts = $state<Array<{ role: Role; text: string; at: number }>>([]);
	let sttProvider = $state('');
	let ttsProvider = $state('');
	let providers = $state({ stt: '', tts: '' });

	function detectDefaultMode(): 'vad' | 'push-to-talk' {
		if (typeof window === 'undefined') return 'vad';
		const touch = window.matchMedia('(pointer: coarse)').matches;
		const narrow = window.innerWidth < 1024;
		return touch && narrow ? 'push-to-talk' : 'vad';
	}

	onDestroy(() => {
		session?.disconnect();
	});

	async function startCall() {
		errorMsg = '';
		transcripts = [];

		session = new RealtimeVoiceSession(
			{ projectId, sessionId, systemPrompt },
			{
				onState: (s) => {
					state = s;
					onStateChange?.(s);
				},
				onTranscript: (text, isFinal, role) => {
					if (!isFinal) return;
					transcripts = [...transcripts, { role, text, at: Date.now() }];
					onTranscript?.(text, role);
				},
				onError: (e) => {
					errorMsg = e.message;
					onError?.(e);
				}
			}
		);

		try {
			await session.connect();
			sttProvider = session.sttProvider;
			ttsProvider = session.ttsProvider;
			providers = { stt: sttProvider, tts: ttsProvider };
			// Push-to-talk starts muted; user presses to speak.
			if (mode === 'push-to-talk') {
				muted = true;
				session.setMuted(true);
			}
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : String(e);
		}
	}

	async function endCall() {
		await session?.disconnect();
		session = null;
	}

	function toggleMute() {
		muted = !muted;
		session?.setMuted(muted);
	}

	function toggleMode() {
		mode = mode === 'vad' ? 'push-to-talk' : 'vad';
		if (mode === 'push-to-talk') {
			muted = true;
			session?.setMuted(true);
		} else {
			muted = false;
			session?.setMuted(false);
		}
	}

	// Push-to-talk handlers: hold down to speak.
	function pttDown() {
		if (mode !== 'push-to-talk' || !session) return;
		muted = false;
		session.setMuted(false);
	}

	function pttUp() {
		if (mode !== 'push-to-talk' || !session) return;
		muted = true;
		session.setMuted(true);
	}

	let active = $derived(
		state === 'connected' ||
			state === 'listening' ||
			state === 'thinking' ||
			state === 'speaking'
	);
</script>

<div class="panel" data-state={state}>
	<header class="panel-header">
		<div class="orb" data-state={state} aria-hidden="true">
			<div class="orb-inner"></div>
		</div>
		<div class="meta">
			<div class="state-label">
				{stateLabel(state)}
			</div>
			{#if providers.stt && providers.tts}
				<div class="providers">
					<span>STT: {providers.stt}</span>
					<span>TTS: {providers.tts}</span>
				</div>
			{/if}
		</div>
	</header>

	{#if errorMsg}
		<div class="error">{errorMsg}</div>
	{/if}

	<div class="actions">
		{#if !active && state !== 'connecting'}
			<button class="btn primary" onclick={startCall}>Start voice call</button>
		{/if}

		{#if active}
			{#if mode === 'vad'}
				<button class="btn" onclick={toggleMute}>
					{muted ? 'Unmute' : 'Mute'}
				</button>
			{:else}
				<button
					class="btn ptt"
					class:pressed={!muted}
					onpointerdown={pttDown}
					onpointerup={pttUp}
					onpointercancel={pttUp}
					onpointerleave={pttUp}
				>
					{muted ? 'Hold to talk' : 'Listening…'}
				</button>
			{/if}

			<button class="btn" onclick={toggleMode} title="Switch input mode">
				{mode === 'vad' ? 'VAD' : 'PTT'}
			</button>

			<button class="btn destructive" onclick={endCall}>Hang up</button>
		{/if}

		{#if state === 'connecting'}
			<span class="loading">Connecting…</span>
		{/if}
	</div>

	{#if transcripts.length > 0}
		<section class="transcript" aria-live="polite">
			{#each transcripts as t}
				<div class="line role-{t.role}">
					<span class="role-tag">{t.role}</span>
					<span class="text">{t.text}</span>
				</div>
			{/each}
		</section>
	{/if}
</div>

<script module lang="ts">
	function stateLabel(s: string) {
		switch (s) {
			case 'idle':
				return 'Ready';
			case 'connecting':
				return 'Connecting';
			case 'connected':
			case 'listening':
				return 'Listening';
			case 'thinking':
				return 'Thinking';
			case 'speaking':
				return 'Speaking';
			case 'reconnecting':
				return 'Reconnecting';
			case 'disconnected':
				return 'Ended';
			case 'error':
				return 'Error';
			default:
				return s;
		}
	}
</script>

<style>
	.panel {
		display: flex;
		flex-direction: column;
		gap: 16px;
		padding: 20px;
		background: var(--card, #fff);
		border: 1px solid var(--border, #e5e5e5);
		border-radius: 12px;
	}

	.panel-header {
		display: flex;
		gap: 16px;
		align-items: center;
	}

	.orb {
		width: 56px;
		height: 56px;
		position: relative;
		flex-shrink: 0;
	}

	.orb-inner {
		position: absolute;
		inset: 0;
		border-radius: 50%;
		background: radial-gradient(
			circle at 35% 30%,
			var(--muted-foreground, #aaa),
			var(--muted, #ccc)
		);
		transition: background 200ms ease, transform 200ms ease;
	}

	.orb[data-state='listening'] .orb-inner,
	.orb[data-state='connected'] .orb-inner {
		background: radial-gradient(circle at 35% 30%, #4ade80, #22c55e);
		animation: pulse 1.4s ease-in-out infinite;
	}

	.orb[data-state='thinking'] .orb-inner {
		background: radial-gradient(circle at 35% 30%, #fbbf24, #f59e0b);
		animation: spin 2s linear infinite;
	}

	.orb[data-state='speaking'] .orb-inner {
		background: radial-gradient(circle at 35% 30%, #60a5fa, #3b82f6);
		animation: pulse 0.8s ease-in-out infinite;
	}

	.orb[data-state='error'] .orb-inner {
		background: radial-gradient(circle at 35% 30%, #f87171, #ef4444);
	}

	@keyframes pulse {
		0%,
		100% {
			transform: scale(1);
		}
		50% {
			transform: scale(1.08);
		}
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}

	.state-label {
		font-size: 1rem;
		font-weight: 600;
		color: var(--foreground, #1a1a1a);
	}

	.providers {
		display: flex;
		gap: 10px;
		font-size: 0.75rem;
		color: var(--muted-foreground, #888);
		margin-top: 2px;
	}

	.error {
		padding: 10px 14px;
		background: color-mix(in oklch, var(--destructive, #ef4444) 10%, transparent);
		color: var(--destructive, #ef4444);
		border-radius: 8px;
		font-size: 0.875rem;
	}

	.actions {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}

	.btn {
		min-height: 44px; /* mobile tap target */
		padding: 10px 18px;
		border: 1px solid var(--border, #ddd);
		background: var(--card, #fff);
		color: var(--foreground, #1a1a1a);
		border-radius: 8px;
		cursor: pointer;
		font-size: 0.875rem;
		font-weight: 500;
		user-select: none;
		touch-action: manipulation;
	}

	.btn.primary {
		background: var(--primary, #000);
		color: var(--primary-foreground, #fff);
		border-color: transparent;
	}

	.btn.destructive {
		background: var(--destructive, #ef4444);
		color: #fff;
		border-color: transparent;
	}

	.btn.ptt {
		flex: 1;
		min-width: 140px;
		transition: transform 100ms ease, background 100ms ease;
	}

	.btn.ptt.pressed {
		background: var(--primary, #000);
		color: var(--primary-foreground, #fff);
		transform: scale(0.98);
	}

	.loading {
		font-size: 0.875rem;
		color: var(--muted-foreground, #888);
	}

	.transcript {
		display: flex;
		flex-direction: column;
		gap: 6px;
		max-height: 260px;
		overflow-y: auto;
		padding-right: 4px;
	}

	.line {
		display: flex;
		gap: 8px;
		font-size: 0.875rem;
		line-height: 1.4;
	}

	.role-tag {
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground, #888);
		min-width: 64px;
		padding-top: 2px;
	}

	.line.role-assistant .role-tag {
		color: var(--primary, #2563eb);
	}
</style>
