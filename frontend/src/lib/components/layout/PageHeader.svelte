<script lang="ts">
	/**
	 * PageHeader - Codex-style thin header bar for the main content panel
	 *
	 * Sits at the top of MainPanel.
	 *
	 * Layout:
	 * - Desktop: a single row. Left cluster (sidebar toggle, profile selector,
	 *   download) sits at the start; git actions sit at the right edge; the chat
	 *   title is an in-flow flex item filling the space between the two clusters —
	 *   it centers itself and shrinks/ellipsizes so it can never overlap the side
	 *   clusters on narrow screens.
	 * - Mobile: a single row. The chat name moves to the global app bar
	 *   (AppTopBar), so the title is hidden here; the row carries download pinned
	 *   at the start with the git actions in a horizontal scroll rail filling the
	 *   rest. Both <div class="hdr-row*"> wrappers use `display:contents` so all
	 *   items share one flex row on every width.
	 *
	 * The project/worktree context badge that used to render here now lives in the
	 * ContextPillBar above the chat input, alongside the profile + branch pills.
	 */
	import { Mic } from 'lucide-svelte';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import { globalSttState, triggerToggle } from '$lib/services/globalStt';

	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		isMobile?: boolean;
		hideGlobalStt?: boolean;
		/** Profile selector — left cluster (desktop) / bottom rail start (mobile). */
		profile?: Snippet;
		/** Left-cluster meta actions (e.g. download chat history). */
		leftActions?: Snippet;
		/** Right cluster / bottom rail: git actions, diff stats, etc. */
		actions?: Snippet;
	}

	let {
		title,
		isMobile = false,
		hideGlobalStt = false,
		profile,
		leftActions,
		actions
	}: Props = $props();

	let showStt = $derived(isMobile && $hotkeyPreferences.globalSttEnabled && !hideGlobalStt);
</script>

<header class="page-header">
	<!-- Row 1: title + meta (static row on mobile; flattened on desktop) -->
	<div class="hdr-row1">
		<h1 class="header-title">{title}</h1>

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
	</div>

	<!-- Row 2: profile + git actions (scroll rail on mobile; flattened on desktop) -->
	<div class="hdr-row2">
		{#if profile}
			<div class="hdr-profile">
				{@render profile()}
			</div>
		{/if}

		<div class="hdr-right">
			{#if actions}
				{@render actions()}
			{/if}
		</div>
	</div>
</header>

<style>
	.page-header {
		position: relative;
		display: flex;
		align-items: center;
		min-height: 40px;
		flex-shrink: 0;
		padding: 4px 12px;
		gap: 8px;
		background: var(--background);
		border-bottom: 1px solid var(--border);
	}

	/* Desktop default: flatten both row wrappers so all items live on one flex row. */
	.hdr-row1,
	.hdr-row2 {
		display: contents;
	}

	.hdr-meta {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}

	.hdr-profile {
		display: flex;
		align-items: center;
		flex-shrink: 0;
	}

	.hdr-right {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}

	/* Desktop ordering across the flattened single row:
	   [profile] [meta] ...[title fills/centers]... [git/right]
	   The title is an in-flow flex item that occupies the space between the
	   side clusters. It grows to centre itself and shrinks (ellipsising) so it
	   can never overlap the profile selector or git actions on narrow screens. */
	.hdr-profile { order: 2; }
	.hdr-meta { order: 3; }
	.header-title { order: 4; }
	.hdr-right {
		order: 5;
		justify-content: flex-end;
		flex-wrap: wrap;
	}

	.header-title {
		flex: 1 1 auto;
		min-width: 0;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		text-align: center;
		margin: 0;
		line-height: 1;
		pointer-events: none;
	}

	/* Header action buttons (mic) */
	.header-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
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

	/* ===================================================================
	   Mobile: a single row. The chat name now lives in the global app bar
	   (AppTopBar), so the title is hidden here and the row carries the
	   original actions — download pinned at the start, with
	   the git actions in a horizontal touch-scroll rail filling the rest.
	   =================================================================== */
	/* Mobile breakpoint: 1080px covers phones AND unfolded foldables (Z Fold 6/7
	   inner ~1028–1040px). Kept in sync with AppLayout's isMobile width cutoff. */
	@media (max-width: 1080px) {
		.page-header {
			flex-direction: row;
			align-items: center;
			min-height: 44px;
			padding: 0 8px;
			gap: 8px;
			/* Only the git rail (.hdr-right) scrolls. */
			overflow: visible;
		}

		/* Flatten both wrappers so every item shares the single row (as on desktop). */
		.hdr-row1,
		.hdr-row2 {
			display: contents;
		}

		/* Chat name is shown in the global app bar on mobile. */
		.header-title {
			display: none;
		}

		/* Pinned, non-scrolling meta actions such as download. */
		.hdr-meta {
			order: 1;
			flex-shrink: 0;
			margin: 0;
		}

		.hdr-profile {
			order: 2;
			flex-shrink: 0;
			margin: 0;
		}

		/* Git actions: horizontal touch-scroll rail filling the remaining width.
		   Editable-prompt popovers inside it switch to position:fixed on mobile,
		   so this overflow doesn't clip them. */
		.hdr-right {
			order: 3;
			flex: 1 1 auto;
			min-width: 0;
			flex-wrap: nowrap;
			/* Reset the desktop `flex-end`: with flex-end + overflow-x the content
			   overflows past the START (left) edge, which is unreachable by scroll —
			   so the git actions slide under the pinned meta-actions cluster
			   instead of scrolling. flex-start sends the overflow to the END so the
			   rail scrolls left→right and never overlaps the meta cluster. */
			justify-content: flex-start;
			margin: 0;
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

		.header-btn {
			width: 36px;
			height: 36px;
		}
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
