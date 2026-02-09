<script lang="ts">
	/**
	 * FeaturePageLayout - Consistent wrapper for all feature pages
	 *
	 * Provides a standardized layout structure:
	 * - FeaturePageHeader (fixed at top)
	 * - Scrollable content area (centered, max-width constrained)
	 * - Optional sticky footer (for actions like Save, Create, etc.)
	 *
	 * Used by Settings, Profiles, Projects, Plugins, and other feature pages
	 * to ensure visual consistency across the application.
	 */
	import FeaturePageHeader from './FeaturePageHeader.svelte';
	import type { Snippet } from 'svelte';
	import '../deck/cards/card-design-system.css';

	interface Props {
		/** Icon component to display in the header */
		icon: any;
		/** Page title displayed in the header */
		title: string;
		/** Optional count of items (e.g., "3 profiles") */
		itemCount?: number;
		/** Label for items (e.g., "profiles", "plugins") */
		itemLabel?: string;
		/** Breadcrumb navigation entries */
		breadcrumbs?: { label: string; onclick?: () => void }[];
		/** Subtitle text displayed below the title */
		subtitle?: string;
		/** Snippet for action buttons rendered in the header */
		headerActions?: Snippet;
		/** Snippet for tab navigation rendered below the header */
		headerTabs?: Snippet;
		/** Main page content (scrollable) */
		children: Snippet;
		/** Optional footer snippet for sticky bottom actions */
		footer?: Snippet;
		/** Maximum width for the content area (default: 'none') */
		maxWidth?: string;
		/** Disable default padding for pages that manage their own layout */
		noPadding?: boolean;
	}

	let {
		icon,
		title,
		itemCount,
		itemLabel,
		breadcrumbs,
		subtitle,
		headerActions,
		headerTabs,
		children,
		footer,
		maxWidth = 'none',
		noPadding = false
	}: Props = $props();
</script>

<div class="feature-page card-system">
	<FeaturePageHeader
		{icon}
		{title}
		{itemCount}
		{itemLabel}
		{breadcrumbs}
		{subtitle}
		tabs={headerTabs}
	>
		{#snippet actions()}
			{#if headerActions}{@render headerActions()}{/if}
		{/snippet}
	</FeaturePageHeader>

	<div class="feature-page-content" class:no-padding={noPadding}>
		<div class="feature-page-content-inner" style:max-width={maxWidth}>
			{@render children()}
		</div>
	</div>

	{#if footer}
		<div class="feature-page-footer">
			<div class="feature-page-footer-inner" style:max-width={maxWidth}>
				{@render footer()}
			</div>
		</div>
	{/if}
</div>

<style>
	.feature-page {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
		background: var(--background);
	}

	.feature-page-content {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
	}

	.feature-page-content-inner {
		margin: 0 auto;
		padding: var(--space-5);
		display: flex;
		flex-direction: column;
		gap: var(--space-5);
	}

	.feature-page-content.no-padding .feature-page-content-inner {
		padding: 0;
		max-width: none !important;
	}

	.feature-page-footer {
		flex-shrink: 0;
		border-top: 1px solid var(--border-subtle);
		background: var(--surface-0);
	}

	.feature-page-footer-inner {
		margin: 0 auto;
		padding: var(--space-3) var(--space-5);
		display: flex;
		justify-content: flex-end;
		gap: var(--space-2);
	}

	/* Scrollbar styling */
	.feature-page-content::-webkit-scrollbar {
		width: 6px;
	}

	.feature-page-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.feature-page-content::-webkit-scrollbar-thumb {
		background: var(--border-default);
		border-radius: 3px;
	}

	.feature-page-content::-webkit-scrollbar-thumb:hover {
		background: var(--text-tertiary);
	}

	/* Mobile responsive adjustments */
	@media (max-width: 639px) {
		.feature-page-content-inner {
			padding: var(--space-3);
		}

		.feature-page-footer-inner {
			padding: var(--space-3);
		}
	}
</style>
