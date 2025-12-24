<script lang="ts">
	/**
	 * ActivityRail - Floating pill navigation bar for The Deck
	 *
	 * Displays mode buttons with active states, badges, and tooltips.
	 * Floats as a horizontal pill bar with glassmorphism effect.
	 */

	import { Plus, Monitor, Bot, Palette, FolderOpen, Settings } from 'lucide-svelte';
	import type { ActivityMode, ActivityBadges } from './types';

	interface Props {
		activeMode: ActivityMode;
		badges?: ActivityBadges;
		isMobile?: boolean;
		onModeChange?: (mode: ActivityMode) => void;
		onLogoClick?: () => void;
		onSettingsClick?: () => void;
	}

	let {
		activeMode,
		badges = {},
		isMobile = false,
		onModeChange,
		onLogoClick,
		onSettingsClick
	}: Props = $props();

	// Activity button configurations with colors
	const activities: Array<{
		mode: ActivityMode;
		label: string;
		color: string;
	}> = [
		{ mode: 'workspace', label: 'Workspace', color: '#3b82f6' },
		{ mode: 'agents', label: 'Agents', color: '#10b981' },
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

<div class="activity-pill" class:mobile={isMobile}>
	<!-- Plus button -->
	<button
		class="plus-button"
		onclick={() => onLogoClick?.()}
		title="New"
	>
		<Plus size={20} strokeWidth={2.5} />
		<span class="tooltip">New</span>
	</button>

	<!-- Divider -->
	<div class="divider"></div>

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
				<div class="icon-wrapper">
					{#if activity.mode === 'workspace'}
						<Monitor size={20} strokeWidth={1.5} />
					{:else if activity.mode === 'agents'}
						<Bot size={20} strokeWidth={1.5} />
					{:else if activity.mode === 'studio'}
						<Palette size={20} strokeWidth={1.5} />
					{:else if activity.mode === 'files'}
						<FolderOpen size={20} strokeWidth={1.5} />
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

	<!-- Divider -->
	<div class="divider"></div>

	<!-- Settings button -->
	<button
		class="settings-button"
		onclick={() => onSettingsClick?.()}
		title="Settings"
	>
		<Settings size={18} strokeWidth={1.5} />
		<span class="tooltip">Settings</span>
	</button>
</div>

<style>
	.activity-pill {
		display: flex;
		flex-direction: row;
		align-items: center;
		height: 48px;
		background: color-mix(in srgb, var(--card) 85%, transparent);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
		border: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
		border-radius: 24px;
		padding: 0 8px;
		gap: 4px;
		box-shadow:
			0 4px 24px -4px rgba(0, 0, 0, 0.15),
			0 0 0 1px rgba(255, 255, 255, 0.05) inset;
	}

	.activity-pill.mobile {
		height: 52px;
		border-radius: 26px;
		padding: 0 10px;
		gap: 6px;
	}

	.divider {
		width: 1px;
		height: 24px;
		background: var(--border);
		opacity: 0.5;
		margin: 0 4px;
	}

	.mobile .divider {
		height: 28px;
		margin: 0 6px;
	}

	.plus-button {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		background: var(--primary);
		border: none;
		border-radius: 50%;
		color: var(--primary-foreground);
		cursor: pointer;
		transition: all 0.2s ease;
		flex-shrink: 0;
	}

	.plus-button:hover {
		transform: scale(1.08);
		box-shadow: 0 0 16px color-mix(in srgb, var(--primary) 50%, transparent);
	}

	.plus-button:active {
		transform: scale(0.95);
	}

	.mobile .plus-button {
		width: 38px;
		height: 38px;
	}

	.activities {
		display: flex;
		flex-direction: row;
		gap: 2px;
	}

	.mobile .activities {
		gap: 4px;
	}

	.activity-button {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.activity-button:hover {
		color: var(--foreground);
		background: color-mix(in srgb, var(--foreground) 8%, transparent);
	}

	.activity-button.active {
		color: var(--activity-color);
		background: color-mix(in srgb, var(--activity-color) 15%, transparent);
	}

	.activity-button.active .icon-wrapper {
		filter: drop-shadow(0 0 6px var(--activity-color));
	}

	.mobile .activity-button {
		width: 42px;
		height: 42px;
	}

	.icon-wrapper {
		display: flex;
		align-items: center;
		justify-content: center;
		transition: filter 0.15s ease;
	}

	.badge {
		position: absolute;
		top: 2px;
		right: 2px;
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
		box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
	}

	.badge.dot {
		width: 8px;
		height: 8px;
		min-width: 8px;
		padding: 0;
		top: 6px;
		right: 6px;
	}

	.tooltip {
		position: absolute;
		left: 50%;
		bottom: calc(100% + 12px);
		transform: translateX(-50%);
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

	.plus-button:hover .tooltip,
	.activity-button:hover .tooltip,
	.settings-button:hover .tooltip {
		opacity: 1;
		visibility: visible;
	}

	/* Disable tooltips on mobile - touch hover states cause issues */
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
		width: 36px;
		height: 36px;
		background: transparent;
		border: none;
		border-radius: 50%;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
		flex-shrink: 0;
	}

	.settings-button:hover {
		color: var(--foreground);
		background: color-mix(in srgb, var(--foreground) 8%, transparent);
	}

	.mobile .settings-button {
		width: 38px;
		height: 38px;
	}
</style>
