<script lang="ts">
	/**
	 * FeaturePageHeader - Unified header for all feature pages (non-chat)
	 *
	 * Replaces per-card internal headers with a consistent, richer header
	 * that integrates with the card design system tokens.
	 *
	 * Features:
	 * - Icon + title + item count display
	 * - Breadcrumb navigation for detail/edit views
	 * - Right-side slot for contextual action buttons
	 * - Optional tab bar below the main row (pill-style segmented control)
	 * - Subtle gradient accent border at the bottom
	 * - Smooth transitions when switching views
	 */
	import type { Snippet } from 'svelte';
	import { PanelLeftOpen, Mic } from 'lucide-svelte';
	import { layout, sidebarCollapsed } from '$lib/stores/layout';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences';
	import { globalSttState, triggerToggle } from '$lib/services/globalStt';
	import '$lib/components/deck/cards/card-design-system.css';

	interface Props {
		icon: any;
		title: string;
		itemCount?: number;
		itemLabel?: string;
		breadcrumbs?: { label: string; onclick?: () => void }[];
		subtitle?: string;
		actions?: Snippet;
		tabs?: Snippet;
	}

	let {
		icon,
		title,
		itemCount,
		itemLabel,
		breadcrumbs,
		subtitle,
		actions,
		tabs
	}: Props = $props();
</script>

<header class="feature-page-header card-system">
	<div class="fph-main">
		<div class="fph-left">
			<button
				class="fph-sidebar-toggle"
				class:desktop-hidden={!$sidebarCollapsed}
				onclick={(e) => { e.stopPropagation(); layout.toggleSidebar(); }}
				title={$sidebarCollapsed ? 'Open sidebar' : 'Close sidebar'}
				aria-label={$sidebarCollapsed ? 'Open sidebar' : 'Close sidebar'}
			>
				<PanelLeftOpen size={16} strokeWidth={1.5} />
			</button>
			{#if breadcrumbs && breadcrumbs.length > 0}
				<nav class="fph-breadcrumbs" aria-label="Breadcrumb">
					{#each breadcrumbs as crumb, i}
						{#if i > 0}
							<span class="fph-breadcrumb-sep" aria-hidden="true">/</span>
						{/if}
						{#if crumb.onclick && i < breadcrumbs.length - 1}
							<button
								class="fph-breadcrumb"
								onclick={crumb.onclick}
								type="button"
							>
								{crumb.label}
							</button>
						{:else}
							<span
								class="fph-breadcrumb fph-breadcrumb--current"
								aria-current={i === breadcrumbs.length - 1 ? 'page' : undefined}
							>
								{crumb.label}
							</span>
						{/if}
					{/each}
				</nav>
				{#if subtitle}
					<span class="fph-subtitle">{subtitle}</span>
				{/if}
			{:else}
				<div class="fph-icon" aria-hidden="true">
					<svelte:component this={icon} size={18} strokeWidth={1.5} />
				</div>
				<h1 class="fph-title">{title}</h1>
				{#if itemCount !== undefined}
					<span class="fph-count">{itemCount} {itemLabel || ''}</span>
				{/if}
				{#if subtitle}
					<span class="fph-subtitle">{subtitle}</span>
				{/if}
			{/if}
		</div>
		<div class="fph-right">
			{#if $hotkeyPreferences.globalSttEnabled}
				<button
					class="fph-stt-btn"
					class:recording={$globalSttState.isRecording}
					class:transcribing={$globalSttState.isTranscribing}
					class:preparing={$globalSttState.isPreparing}
					onclick={triggerToggle}
					aria-label={$globalSttState.isRecording ? 'Stop recording' : 'Start voice input'}
				>
					<Mic size={18} />
				</button>
			{/if}
			{#if actions}
				{@render actions()}
			{/if}
		</div>
	</div>
	{#if tabs}
		<div class="fph-tabs-row">
			{@render tabs()}
		</div>
	{/if}
</header>

<style>
	/* -----------------------------------------------------------------------
	   FEATURE PAGE HEADER
	   ----------------------------------------------------------------------- */
	.feature-page-header {
		background: var(--surface-0);
		border-bottom: 1px solid var(--border-subtle);
		position: relative;
		flex-shrink: 0;
	}

	/* Subtle gradient accent line at the very bottom */
	.feature-page-header::after {
		content: '';
		position: absolute;
		bottom: 0;
		left: 0;
		right: 0;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent 0%,
			color-mix(in oklch, var(--primary) 8%, transparent) 30%,
			color-mix(in oklch, var(--primary) 12%, transparent) 50%,
			color-mix(in oklch, var(--primary) 8%, transparent) 70%,
			transparent 100%
		);
		pointer-events: none;
	}

	/* -----------------------------------------------------------------------
	   MAIN ROW
	   ----------------------------------------------------------------------- */
	.fph-main {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 56px;
		padding: 0 var(--space-5);
		gap: var(--space-3);
	}

	/* -----------------------------------------------------------------------
	   LEFT SIDE
	   ----------------------------------------------------------------------- */
	.fph-left {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		min-width: 0;
		flex: 1;
	}

	/* Icon container - 32px tinted square */
	.fph-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		flex-shrink: 0;
		border-radius: var(--radius-sm);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
		transition: background var(--transition-smooth), color var(--transition-smooth);
	}

	/* Page title */
	.fph-title {
		font-size: var(--text-lg);
		font-weight: 600;
		color: var(--text-primary);
		letter-spacing: var(--tracking-tight);
		margin: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		line-height: 1.2;
	}

	/* Item count badge */
	.fph-count {
		font-size: var(--text-sm);
		color: var(--text-tertiary);
		white-space: nowrap;
		flex-shrink: 0;
	}

	/* Subtitle (file path, etc.) */
	.fph-subtitle {
		font-size: var(--text-xs);
		font-family: var(--card-font-mono);
		color: var(--text-tertiary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		min-width: 0;
	}

	/* -----------------------------------------------------------------------
	   BREADCRUMBS
	   ----------------------------------------------------------------------- */
	.fph-breadcrumbs {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
	}

	/* Breadcrumb items (both button and span) */
	.fph-breadcrumb {
		font-size: var(--text-sm);
		color: var(--text-tertiary);
		padding: 2px 4px;
		border-radius: var(--radius-sm);
		white-space: nowrap;
		transition: color var(--transition-fast), background var(--transition-fast);
	}

	/* Clickable breadcrumb buttons */
	button.fph-breadcrumb {
		background: none;
		border: none;
		cursor: pointer;
		font: inherit;
	}

	button.fph-breadcrumb:hover {
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}

	button.fph-breadcrumb:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Current (last) breadcrumb - not clickable */
	.fph-breadcrumb--current {
		color: var(--text-primary);
		font-weight: 500;
	}

	/* Separator slash */
	.fph-breadcrumb-sep {
		font-size: var(--text-xs);
		color: var(--text-tertiary);
		opacity: 0.5;
		flex-shrink: 0;
		user-select: none;
	}

	/* -----------------------------------------------------------------------
	   RIGHT SIDE (action buttons slot)
	   ----------------------------------------------------------------------- */
	.fph-right {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		flex-shrink: 0;
	}

	/* -----------------------------------------------------------------------
	   TAB BAR ROW
	   ----------------------------------------------------------------------- */
	.fph-tabs-row {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 0 var(--space-5) var(--space-2) var(--space-5);
	}

	/* -----------------------------------------------------------------------
	   SIDEBAR TOGGLE
	   ----------------------------------------------------------------------- */
	.fph-sidebar-toggle {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--text-secondary);
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
	}

	.fph-sidebar-toggle:hover {
		background: var(--surface-hover);
		color: var(--text-primary);
	}

	.fph-sidebar-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* On desktop, hide toggle when sidebar is open */
	.fph-sidebar-toggle.desktop-hidden {
		display: none;
	}

	/* -----------------------------------------------------------------------
	   DESKTOP CENTERING — raised to 1024px so foldable/tablet touch devices
	   (640–1023px) use simpler space-between flexbox instead of absolute
	   positioning, which causes overlap on those screens.
	   ----------------------------------------------------------------------- */
	@media (min-width: 1024px) {
		.fph-main {
			position: relative;
		}

		.fph-left {
			position: absolute;
			left: 0;
			right: 0;
			justify-content: center;
			pointer-events: none;
		}

		.fph-left > :global(*) {
			pointer-events: auto;
		}

		/* Keep sidebar toggle at the left edge when visible */
		.fph-sidebar-toggle:not(.desktop-hidden) {
			position: absolute;
			left: var(--space-5);
		}

		.fph-right {
			position: relative;
			z-index: 1;
			margin-left: auto;
		}
	}

	/* -----------------------------------------------------------------------
	   RESPONSIVE & ACCESSIBILITY
	   ----------------------------------------------------------------------- */

	/* Ensure touch targets on mobile */
	@media (max-width: 639px) {
		.fph-main {
			padding: 0 var(--space-3);
			gap: var(--space-2);
			height: auto;
			min-height: 48px;
			flex-wrap: wrap;
			padding-top: var(--space-2);
			padding-bottom: var(--space-2);
		}

		.fph-left {
			flex: 1 1 0;
			min-width: 0;
			overflow: hidden;
		}

		.fph-right {
			flex: 1 0 100%;
			flex-wrap: wrap;
			gap: var(--space-2);
		}

		/* When only the STT button is present (no actions), keep it inline */
		.fph-right:not(:has(> :nth-child(2))) {
			flex: 0 0 auto;
		}

		.fph-tabs-row {
			padding: 0 var(--space-3) var(--space-2) var(--space-3);
			overflow-x: auto;
			-webkit-overflow-scrolling: touch;
		}

		.fph-sidebar-toggle {
			width: 44px;
			height: 44px;
		}

		/* On mobile, always show the sidebar toggle */
		.fph-sidebar-toggle.desktop-hidden {
			display: flex;
		}

		button.fph-breadcrumb {
			min-height: 44px;
			display: flex;
			align-items: center;
		}

		.fph-title {
			font-size: var(--text-base);
			overflow: hidden;
			text-overflow: ellipsis;
		}

		.fph-count {
			font-size: var(--text-xs);
		}

		.fph-subtitle {
			display: none;
		}
	}

	/* -----------------------------------------------------------------------
	   GLOBAL STT BUTTON (mobile only)
	   ----------------------------------------------------------------------- */
	.fph-stt-btn {
		display: none;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--text-secondary);
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}

	.fph-stt-btn:hover {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
	}

	.fph-stt-btn.recording {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		animation: fph-pulse-recording 1.5s ease-in-out infinite;
	}

	.fph-stt-btn.transcribing {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		animation: fph-pulse-transcribing 0.8s ease-in-out infinite;
	}

	.fph-stt-btn.preparing {
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		color: var(--text-tertiary);
		animation: fph-pulse-transcribing 1s ease-in-out infinite;
	}

	@keyframes fph-pulse-recording {
		0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 color-mix(in oklch, var(--destructive) 40%, transparent); }
		50% { transform: scale(1.05); box-shadow: 0 0 0 4px transparent; }
	}

	@keyframes fph-pulse-transcribing {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.6; }
	}

	@media (max-width: 639px) {
		.fph-stt-btn {
			display: flex;
			width: 44px;
			height: 44px;
		}
	}

	/* Reduced motion */
	@media (prefers-reduced-motion: reduce) {
		.fph-icon,
		.fph-breadcrumb,
		.fph-sidebar-toggle,
		.fph-stt-btn {
			transition: none;
		}

		.fph-stt-btn.recording,
		.fph-stt-btn.transcribing,
		.fph-stt-btn.preparing {
			animation: none;
		}
	}
</style>
