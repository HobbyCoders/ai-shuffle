<script lang="ts">
	/**
	 * PageHeader - thin header row over MainPanel in the mobile app.
	 *
	 * The chat name stays in AppTopBar above this row. Transcript download is
	 * pinned left and the Git dropdown is pinned right. Neither action region
	 * clips popovers; the mobile Git panel remains a bottom sheet at every width.
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
		/** Right-aligned Git dropdown and related actions. */
		actions?: Snippet;
	}

	let { hideGlobalStt = false, leftActions, actions }: Props = $props();

	let showStt = $derived($hotkeyPreferences.globalSttEnabled && !hideGlobalStt);
</script>

<header class="page-header" aria-label="Chat actions">
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
		min-height: 52px;
		flex-shrink: 0;
		padding: 0 8px;
		gap: 8px;
		background: var(--background);
		border-bottom: 1px solid var(--border);
		/* Keep the Git sheet and its dialogs outside clipping containers. */
		overflow: visible;
	}

	/* Pinned, non-scrolling meta actions such as download. */
	.hdr-meta {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}

	/* The remaining space separates the left download from the right Git button. */
	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1 1 auto;
		min-width: 0;
		flex-wrap: nowrap;
		justify-content: flex-end;
		overflow: visible;
	}

	.hdr-right > :global(*) {
		flex-shrink: 0;
	}

	/* Preserve matching touch targets after moving the actions out of AppTopBar. */
	.page-header :global(.git-btn),
	.page-header :global(.download-jsonl-btn) {
		box-sizing: border-box;
		min-width: 44px;
		min-height: 44px;
		height: 44px;
	}
	.page-header :global(.download-jsonl-btn) { width: 44px; }

	/* Header action buttons (mic) */
	.header-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
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
