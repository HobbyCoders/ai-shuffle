<script lang="ts">
	/**
	 * PageHeader - thin header row over MainPanel in the mobile app.
	 *
	 * The chat name lives in the global app bar (AppTopBar), so this row only
	 * carries actions: the meta actions (voice input, download) pinned at the
	 * start and the git actions in a horizontal touch-scroll rail filling the
	 * rest (chat mode omits the git cluster).
	 *
	 * The project/worktree context badge that used to render here now lives in the
	 * ContextPillBar above the chat input, alongside the profile + branch pills.
	 */
	import { Mic } from 'lucide-svelte';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import { globalSttState, triggerToggle } from '$lib/services/globalStt';

	import type { Snippet } from 'svelte';

	interface Props {
		hideGlobalStt?: boolean;
		/** Pinned meta actions (e.g. download chat history). */
		leftActions?: Snippet;
		/** Scroll rail: git actions, diff stats, etc. */
		actions?: Snippet;
	}

	let { hideGlobalStt = false, leftActions, actions }: Props = $props();

	let showStt = $derived($hotkeyPreferences.globalSttEnabled && !hideGlobalStt);
</script>

<header class="page-header">
	<div class="hdr-meta">
		{#if showStt}
			<button
				class="header-btn mic"
				class:recording={$globalSttState.isRecording}
				class:transcribing={$globalSttState.isTranscribing}
				class:preparing={$globalSttState.isPreparing}
				onclick={triggerToggle}
				aria-label={$globalSttState.isRecording ? 'Stop recording' : 'Start voice input'}
			>
				<Mic size={18} />
			</button>
		{/if}
		{#if leftActions}
			{@render leftActions()}
		{/if}
	</div>

	<div class="hdr-right">
		{#if actions}
			{@render actions()}
		{/if}
	</div>
</header>

<style>
	.page-header {
		position: relative;
		display: flex;
		align-items: center;
		min-height: 44px;
		flex-shrink: 0;
		padding: 0 8px;
		gap: 8px;
		background: var(--background);
		border-bottom: 1px solid var(--border);
		/* Only the git rail (.hdr-right) scrolls. */
		overflow: visible;
	}

	/* Pinned, non-scrolling meta actions such as download. */
	.hdr-meta {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}

	/* Git actions: horizontal touch-scroll rail filling the remaining width.
	   Editable-prompt popovers inside it switch to position:fixed on mobile,
	   so this overflow doesn't clip them. flex-start (not flex-end) sends the
	   overflow to the END, so the rail scrolls left→right and never slides
	   under the pinned meta cluster. */
	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1 1 auto;
		min-width: 0;
		flex-wrap: nowrap;
		justify-content: flex-start;
		overflow-x: auto;
		overflow-y: hidden;
		-webkit-overflow-scrolling: touch;
		scrollbar-width: none; /* Firefox */
	}

	.hdr-right::-webkit-scrollbar {
		display: none;
	}

	.hdr-right > :global(*) {
		flex-shrink: 0;
	}

	/* Header action buttons (mic) */
	.header-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}

	.header-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	/* Mic button */
	.header-btn.mic {
		color: var(--muted-foreground);
	}

	.header-btn.mic:hover {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
	}

	.header-btn.mic.recording {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		animation: pulse-recording 1.5s ease-in-out infinite;
	}

	.header-btn.mic.transcribing {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		animation: pulse-transcribing 0.8s ease-in-out infinite;
	}

	.header-btn.mic.preparing {
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		color: var(--muted-foreground);
		animation: pulse-transcribing 1s ease-in-out infinite;
	}

	@keyframes pulse-recording {
		0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 color-mix(in oklch, var(--destructive) 40%, transparent); }
		50% { transform: scale(1.05); box-shadow: 0 0 0 4px transparent; }
	}

	@keyframes pulse-transcribing {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.6; }
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.header-btn {
			transition: none;
		}
		.header-btn.mic.recording,
		.header-btn.mic.transcribing,
		.header-btn.mic.preparing {
			animation: none;
		}
	}
</style>
