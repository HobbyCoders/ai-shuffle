<script lang="ts">
	/**
	 * ActivityRail - Vertical navigation rail for The Deck
	 *
	 * Displays mode buttons with active states, badges, and tooltips.
	 * Switches to horizontal layout on mobile.
	 */

	import { Plus, Monitor, Palette, FolderOpen, Settings, UserCog } from 'lucide-svelte';
	import type { ActivityMode, ActivityBadges } from './types';

	interface Props {
		activeMode: ActivityMode;
		badges?: ActivityBadges;
		isMobile?: boolean;
		isAdmin?: boolean;
		onModeChange?: (mode: ActivityMode) => void;
		onLogoClick?: () => void;
		onSettingsClick?: () => void;
		onUserSettingsClick?: () => void;
	}

	let {
		activeMode,
		badges = {},
		isMobile = false,
		isAdmin = false,
		onModeChange,
		onLogoClick,
		onSettingsClick,
		onUserSettingsClick
	}: Props = $props();

	// Activity button configurations with colors
	const activities: Array<{
		mode: ActivityMode;
		label: string;
		color: string;
	}> = [
		{ mode: 'workspace', label: 'Workspace', color: '#3b82f6' },
		{ mode: 'studio', label: 'Studio', color: '#a855f7' },
		{ mode: 'files', label: 'Files', color: '#f59e0b' }
	];

	function handleModeClick(mode: ActivityMode) {
		onModeChange?.(mode);
	}

	function getBadge(mode: ActivityMode): number | 'dot' | undefined {
		return badges[mode];
	}
</script>

<div class="activity-rail" class:mobile={isMobile}>
	<!-- Plus button - top on desktop, included in mobile rail -->
	<button
		class="plus-button"
		onclick={() => onLogoClick?.()}
		title="New"
	>
		<Plus size={24} strokeWidth={2} />
		<span class="tooltip">New</span>
	</button>

	<!-- Activity buttons -->
	<div class="activities">
		{#each activities as activity}
			{@const isActive = activeMode === activity.mode}
			{@const badge = getBadge(activity.mode)}
			<button
				class="activity-button"
				class:active={isActive}
				style:--activity-color={activity.color}
				onclick={() => handleModeClick(activity.mode)}
				title={activity.label}
			>
				{#if isActive}
					<div class="active-indicator"></div>
				{/if}
				<div class="icon-wrapper">
					{#if activity.mode === 'workspace'}
						<Monitor size={22} strokeWidth={1.5} />
					{:else if activity.mode === 'studio'}
						<Palette size={22} strokeWidth={1.5} />
					{:else if activity.mode === 'files'}
						<FolderOpen size={22} strokeWidth={1.5} />
					{/if}
				</div>
				{#if badge !== undefined}
					<div class="badge" class:dot={badge === 'dot'}>
						{#if badge !== 'dot'}
							{badge > 99 ? '99+' : badge}
						{/if}
					</div>
				{/if}
				<span class="tooltip">{activity.label}</span>
			</button>
		{/each}
	</div>

	<!-- Settings button at bottom - admin sees Settings, non-admin sees My Settings -->
	<button
		class="settings-button"
		onclick={() => isAdmin ? onSettingsClick?.() : onUserSettingsClick?.()}
		title={isAdmin ? 'Settings' : 'My Settings'}
	>
		{#if isAdmin}
			<Settings size={20} strokeWidth={1.5} />
			<span class="tooltip">Settings</span>
		{:else}
			<UserCog size={20} strokeWidth={1.5} />
			<span class="tooltip">My Settings</span>
		{/if}
	</button>
</div>

<style>
	.activity-rail {
		display: flex;
		flex-direction: column;
		align-items: center;
		width: 64px;
		height: 100%;
		background: color-mix(in srgb, var(--card) 85%, transparent);
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		border-right: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
		padding: 12px 0;
		gap: 8px;
		box-shadow: 2px 0 20px -5px rgba(0, 0, 0, 0.1);
	}

	.activity-rail.mobile {
		flex-direction: row;
		width: 100%;
		height: auto;
		min-height: 64px;
		border-right: none;
		border-top: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
		/* Safe area support for notched devices */
		padding: 8px 16px;
		padding-bottom: max(12px, env(safe-area-inset-bottom, 12px));
		padding-left: max(16px, env(safe-area-inset-left, 16px));
		padding-right: max(16px, env(safe-area-inset-right, 16px));
		justify-content: space-around;
		box-shadow: 0 -2px 20px -5px rgba(0, 0, 0, 0.1);
	}

	.plus-button {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		background: linear-gradient(135deg, var(--primary) 0%, color-mix(in srgb, var(--primary) 80%, #8b5cf6) 100%);
		border: none;
		border-radius: 12px;
		color: var(--primary-foreground);
		cursor: pointer;
		transition: all 0.2s ease;
		margin-bottom: 16px;
		box-shadow: 0 4px 12px -2px color-mix(in srgb, var(--primary) 40%, transparent);
		-webkit-tap-highlight-color: transparent;
	}

	.plus-button:hover {
		transform: scale(1.08);
		box-shadow: 0 6px 20px -2px color-mix(in srgb, var(--primary) 50%, transparent);
	}

	.plus-button:active {
		transform: scale(0.95);
	}

	.mobile .plus-button {
		margin-bottom: 0;
		/* Apple HIG: minimum 44x44px touch target */
		width: 48px;
		height: 48px;
		border-radius: 12px;
	}

	.plus-button .tooltip {
		position: absolute;
		left: calc(100% + 12px);
		top: 50%;
		transform: translateY(-50%);
		padding: 6px 10px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: 6px;
		font-size: 0.75rem;
		color: var(--popover-foreground);
		white-space: nowrap;
		opacity: 0;
		visibility: hidden;
		transition: all 0.15s ease;
		pointer-events: none;
		z-index: 100;
		box-shadow: var(--shadow-m);
	}

	.mobile .plus-button .tooltip {
		left: 50%;
		top: auto;
		bottom: calc(100% + 12px);
		transform: translateX(-50%);
	}

	.plus-button:hover .tooltip {
		opacity: 1;
		visibility: visible;
	}

	.activities {
		display: flex;
		flex-direction: column;
		gap: 4px;
		flex: 1;
	}

	.mobile .activities {
		flex-direction: row;
		flex: initial;
		gap: 4px;
	}

	.activity-button {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 48px;
		height: 48px;
		background: transparent;
		border: none;
		border-radius: 12px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.2s ease;
		-webkit-tap-highlight-color: transparent;
	}

	/* Mobile: ensure 44x44px minimum touch target */
	.mobile .activity-button {
		width: 48px;
		height: 48px;
		min-width: 48px;
		min-height: 48px;
	}

	.activity-button:hover {
		color: var(--foreground);
		background: color-mix(in srgb, var(--foreground) 8%, transparent);
	}

	.activity-button:active {
		transform: scale(0.92);
	}

	.activity-button.active {
		color: var(--activity-color);
		background: color-mix(in srgb, var(--activity-color) 15%, transparent);
		box-shadow: 0 0 16px -2px color-mix(in srgb, var(--activity-color) 30%, transparent);
	}

	.active-indicator {
		position: absolute;
		left: 0;
		top: 50%;
		transform: translateY(-50%);
		width: 3px;
		height: 24px;
		background: var(--activity-color);
		border-radius: 0 2px 2px 0;
		box-shadow: 0 0 12px var(--activity-color), 0 0 24px color-mix(in srgb, var(--activity-color) 60%, transparent);
	}

	.mobile .active-indicator {
		left: 50%;
		top: auto;
		bottom: 0;
		transform: translateX(-50%);
		width: 24px;
		height: 3px;
		border-radius: 2px 2px 0 0;
	}

	.icon-wrapper {
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.badge {
		position: absolute;
		top: 4px;
		right: 4px;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		background: var(--destructive);
		border-radius: 8px;
		font-size: 0.625rem;
		font-weight: 600;
		color: var(--destructive-foreground);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.badge.dot {
		width: 8px;
		height: 8px;
		min-width: 8px;
		padding: 0;
		top: 8px;
		right: 8px;
	}

	.tooltip {
		position: absolute;
		left: calc(100% + 12px);
		top: 50%;
		transform: translateY(-50%);
		padding: 6px 10px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: 6px;
		font-size: 0.75rem;
		color: var(--popover-foreground);
		white-space: nowrap;
		opacity: 0;
		visibility: hidden;
		transition: all 0.15s ease;
		pointer-events: none;
		z-index: 100;
		box-shadow: var(--shadow-m);
	}

	.mobile .tooltip {
		left: 50%;
		top: auto;
		bottom: calc(100% + 12px);
		transform: translateX(-50%);
	}

	.activity-button:hover .tooltip,
	.settings-button:hover .tooltip {
		opacity: 1;
		visibility: visible;
	}

	/* Disable tooltips on mobile - touch hover states cause layout issues */
	.mobile .activity-button:hover .tooltip,
	.mobile .settings-button:hover .tooltip,
	.mobile .plus-button:hover .tooltip {
		opacity: 0;
		visibility: hidden;
	}

	.settings-button {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		background: transparent;
		border: none;
		border-radius: 10px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.2s ease;
		margin-top: auto;
		-webkit-tap-highlight-color: transparent;
	}

	.settings-button:hover {
		color: var(--foreground);
		background: color-mix(in srgb, var(--foreground) 8%, transparent);
	}

	.settings-button:active {
		transform: scale(0.92);
	}

	/* Mobile-specific settings button styling - ensure 44x44px touch target */
	.mobile .settings-button {
		margin-top: 0;
		width: 48px;
		height: 48px;
		min-width: 48px;
		min-height: 48px;
	}

	.mobile .settings-button .tooltip {
		left: 50%;
		top: auto;
		bottom: calc(100% + 12px);
		transform: translateX(-50%);
	}

	/* Tablet adjustments - medium screens */
	@media (min-width: 600px) and (max-width: 1024px) {
		/* Increase icon sizes for tablet */
		.mobile .plus-button {
			width: 52px;
			height: 52px;
		}

		.mobile .activity-button {
			width: 52px;
			height: 52px;
			min-width: 52px;
			min-height: 52px;
		}

		.mobile .activity-button .icon-wrapper :global(svg) {
			width: 24px;
			height: 24px;
		}

		.mobile .settings-button {
			width: 52px;
			height: 52px;
			min-width: 52px;
			min-height: 52px;
		}

		.mobile .settings-button :global(svg) {
			width: 22px;
			height: 22px;
		}

		.mobile .activities {
			gap: 8px;
		}
	}

	/* Landscape orientation adjustments */
	@media (orientation: landscape) and (max-height: 500px) {
		.mobile {
			min-height: 56px;
			padding: 6px 16px;
			padding-bottom: max(8px, env(safe-area-inset-bottom, 8px));
		}

		.mobile .plus-button,
		.mobile .activity-button,
		.mobile .settings-button {
			width: 44px;
			height: 44px;
			min-width: 44px;
			min-height: 44px;
		}
	}
</style>
