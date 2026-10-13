<script lang="ts">
	import { Mic, LoaderCircle, Square, X } from 'lucide-svelte';
	import { composerDictation } from '$lib/services/composerDictation';
	import { sessions } from '$lib/stores/app-data.svelte';
	import {
		globalSttState,
		globalSttAudio,
		globalSttUpload,
		cancelVoiceInput,
		triggerToggle,
		clearError
	} from '$lib/services/globalStt';

	const state = $derived($globalSttState);
	const busy = $derived(state.isPreparing || state.isRecording || state.isTranscribing);
	const backgroundComposer = $derived(state.source === 'composer' && !!$composerDictation && !$composerDictation.attached);
	const ownerTitle = $derived($sessions.find(s => s.id === $composerDictation?.sessionId)?.title || 'Chat draft');
	const visible = $derived((state.source === 'global' || backgroundComposer) && (busy || !!state.error));
	const transcript = $derived([...state.segments, state.partial].join(' ').trim());
	const label = $derived(state.isPreparing ? 'Getting microphone ready…'
		: state.isRecording ? 'Listening…'
		: state.isTranscribing ? 'Transcribing…' : 'Voice input');

	function dismiss() {
		if (busy) cancelVoiceInput();
		else clearError();
	}

	$effect(() => {
		if (!visible) return;
		const onKey = (event: KeyboardEvent) => {
			if (event.key !== 'Escape') return;
			event.preventDefault();
			event.stopPropagation();
			dismiss();
		};
		window.addEventListener('keydown', onKey, true);
		return () => window.removeEventListener('keydown', onKey, true);
	});
</script>

{#if visible}
	<!-- Keep the focused field and caret in place for automatic insertion. -->
	<aside class="global-voice-input" aria-label={backgroundComposer ? 'Background voice input' : 'Global voice input'}>
		<div class="heading">
			<div class="status" role="status">
				{#if state.isPreparing || state.isTranscribing}
					<LoaderCircle size={18} class="spinner" />
				{:else}
					<Mic size={18} />
				{/if}
				<strong>{label}</strong>
			</div>
			{#if state.isRecording}
				<button type="button" onclick={triggerToggle} aria-label="Finish voice input" title="Finish dictation"><Square size={16} /></button>
			{/if}
			<button type="button" onclick={dismiss} aria-label={busy ? 'Discard voice input' : 'Dismiss voice input error'} title={busy ? 'Discard (Esc)' : 'Dismiss (Esc)'}>
				<X size={16} />
			</button>
		</div>
		{#if backgroundComposer}<p class="hint">Saving to {ownerTitle}. You can keep browsing.</p>{/if}
		{#if state.error}
			<p class="error" role="alert">{state.error}</p>
		{/if}
		{#if state.isRecording}
			<div class="waveform" aria-hidden="true">
				{#each $globalSttAudio.bands as band, index (index)}
					<span style:height="{Math.max(3, Math.min(1, band) * 28)}px"></span>
				{/each}
			</div>
			<p class="hint">Speak naturally. Use Finish to end dictation.</p>
		{:else if state.isPreparing}
			<p class="hint">You can start speaking when “Listening” appears.</p>
		{:else if state.isTranscribing}
			<p class="hint">Finishing your transcript…</p>
			{#if $globalSttUpload !== null}
				<progress aria-label="Uploading recording" max="1" value={$globalSttUpload}></progress>
			{/if}
		{/if}
		{#if transcript}
			<p class="transcript">{transcript}</p>
		{/if}
	</aside>
{/if}

<style>
	.global-voice-input {
		position: fixed;
		z-index: 1100;
		bottom: max(24px, env(safe-area-inset-bottom));
		left: 50%;
		transform: translateX(-50%);
		width: min(380px, calc(100vw - 32px));
		max-height: calc(100dvh - 48px);
		overflow-y: auto;
		padding: 14px 16px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--background);
		color: var(--foreground);
		box-shadow: 0 8px 32px #0003;
	}
	.heading, .status { display: flex; align-items: center; gap: 8px; }
	.heading { justify-content: space-between; }
	.status { color: var(--primary); font-size: 14px; }
	button { display: grid; place-items: center; flex-shrink: 0; width: 32px; height: 32px; border: 0; border-radius: var(--radius); background: transparent; color: var(--muted-foreground); cursor: pointer; }
	button:hover { background: var(--muted); color: var(--foreground); }
	button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	.hint, .error { margin: 8px 0 0; font-size: 12px; line-height: 1.5; }
	.hint { color: var(--muted-foreground); }
	.error { color: var(--destructive); }
	.waveform { display: flex; align-items: center; justify-content: center; gap: 4px; height: 32px; margin-top: 8px; }
	.waveform span { flex: 1; max-width: 5px; border-radius: 999px; background: var(--primary); transition: height 80ms ease; }
	.transcript { max-height: 150px; overflow-y: auto; overflow-wrap: anywhere; margin: 12px 0 0; padding-top: 12px; border-top: 1px solid var(--border); font-size: 14px; line-height: 1.5; }
	progress { width: 100%; height: 4px; margin-top: 8px; accent-color: var(--primary); }
	.status :global(.spinner) { animation: spin 1s linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.status :global(.spinner) { animation: none; }
		.waveform span { transition: none; }
	}
</style>
