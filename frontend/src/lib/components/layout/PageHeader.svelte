<script lang="ts">
	/**
	 * PageHeader - Codex-style thin header bar for the main content panel
	 *
	 * Sits at the top of MainPanel. Shows the current page/chat title,
	 * optional project or worktree context, and a sidebar toggle when
	 * the sidebar is collapsed.
	 */
	import { PanelLeftOpen, GitBranch, FolderClosed, Mic } from 'lucide-svelte';
	import { layout, sidebarCollapsed } from '$lib/stores/layout';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import { globalSttState, triggerToggle } from '$lib/services/globalStt';

	import type { Snippet } from 'svelte';

	interface Props {
		title: string;
		projectName?: string;
		worktreeBranch?: string;
		showSidebarToggle?: boolean;
		isMobile?: boolean;
		hideGlobalStt?: boolean;
		actions?: Snippet;
	}

	let {
		title,
		projectName,
		worktreeBranch,
		showSidebarToggle = false,
		isMobile = false,
		hideGlobalStt = false,
		actions
	}: Props = $props();

	let showToggle = $derived(showSidebarToggle && $sidebarCollapsed);
	let showStt = $derived(isMobile && $hotkeyPreferences.globalSttEnabled && !hideGlobalStt);
	let hasContext = $derived(!!projectName || !!worktreeBranch);
	let hasRightContent = $derived(showStt || !!actions || hasContext);
</script>

<header class="page-header">
	<div class="header-left" class:has-toggle={showToggle}>
		{#if showToggle || isMobile}
			<button
				class="sidebar-toggle"
				onclick={(e) => { e.stopPropagation(); layout.toggleSidebar(); }}
				title="Open sidebar"
				aria-label="Open sidebar"
			>
				<PanelLeftOpen size={16} strokeWidth={1.5} />
			</button>
		{/if}

		<h1 class="header-title">{title}</h1>
	</div>

	{#if hasRightContent}
		<div class="header-right">
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
			{#if worktreeBranch}
				<span class="context-badge" title="{projectName ? projectName + ' · ' : ''}Worktree branch: {worktreeBranch}">
					{#if projectName}
						<FolderClosed size={12} strokeWidth={1.5} />
						<span class="context-label">{projectName}</span>
						<span class="context-separator">/</span>
					{/if}
					<GitBranch size={12} strokeWidth={1.5} />
					<span class="context-label">{worktreeBranch}</span>
				</span>
			{:else if projectName}
				<span class="context-badge" title="Project: {projectName}">
					<FolderClosed size={12} strokeWidth={1.5} />
					<span class="context-label">{projectName}</span>
				</span>
			{/if}
			{#if actions}
				{@render actions()}
			{/if}
		</div>
	{/if}
</header>

<style>
	.page-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		min-height: 40px;
		flex-shrink: 0;
		padding: 4px 12px;
		row-gap: 2px;
		background: var(--background);
		border-bottom: 1px solid var(--border);
	}

	.header-left {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		flex: 1 1 auto;
		min-height: 32px;
	}

	.header-right {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		flex: 1 0 auto;
		min-height: 32px;
	}

	/* Sidebar toggle button */
	.sidebar-toggle {
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
	}

	.sidebar-toggle:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.sidebar-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* Page title */
	.header-title {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		min-width: 0;
		margin: 0;
		line-height: 1;
	}

	/* Project / worktree context badge — styled to match .git-btn */
	.context-badge {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		flex-shrink: 1;
		min-width: 0;
		height: 26px;
		padding: 0 8px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 500;
		max-width: 320px;
	}

	.context-badge :global(svg) {
		flex-shrink: 0;
	}

	.context-label {
		font-size: 0.75rem;
		line-height: 1;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.context-separator {
		font-size: 0.75rem;
		line-height: 1;
		color: var(--muted-foreground);
		opacity: 0.5;
		flex-shrink: 0;
	}

	/* Header action buttons */
	.header-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
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

	/* Desktop layout — title fills available space and truncates,
	   right elements (git actions, context badges) pushed to the right edge. */
	@media (min-width: 1024px) {
		.header-left {
			flex: 1 1 0%;
			min-width: 0;
			justify-content: center;
			position: relative;
		}

		.header-left .sidebar-toggle {
			position: absolute;
			left: 0;
		}

		.header-title {
			text-align: center;
			max-width: 100%;
		}

		.header-right {
			flex: 0 0 auto;
			margin-left: auto;
			flex-wrap: wrap;
			justify-content: flex-end;
		}
	}

	/* Mobile touch targets & wrapping layout */
	@media (max-width: 639px) {
		.page-header {
			position: relative;
			height: auto;
			min-height: 48px;
			flex-wrap: wrap;
			padding: var(--space-2, 8px) var(--space-3, 12px);
			gap: var(--space-2, 8px);
		}

		.header-left {
			flex: 1 1 100%;
			min-width: 0;
			min-height: 36px;
			overflow: hidden;
			justify-content: center;
			padding-left: 36px;
			padding-right: 36px;
		}

		.sidebar-toggle {
			position: absolute;
			left: var(--space-3, 12px);
			top: var(--space-2, 8px);
			width: 36px;
			height: 36px;
		}

		.header-btn {
			width: 36px;
			height: 36px;
		}

		.header-right {
			flex: 1 0 100%;
			flex-wrap: wrap;
			justify-content: center;
			min-height: 36px;
			gap: var(--space-2, 8px);
		}

		/* When only the STT button or context badge is present (no git actions), keep it inline */
		.header-right:not(:has(.git-actions)) {
			flex: 0 0 auto;
		}

		.header-title {
			flex: 1;
			min-width: 0;
			text-align: center;
		}

		.context-badge {
			flex-shrink: 1;
			max-width: none;
			height: 36px;
			padding: 0 12px;
			font-size: 0.75rem;
		}
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.sidebar-toggle {
			transition: none;
		}
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
