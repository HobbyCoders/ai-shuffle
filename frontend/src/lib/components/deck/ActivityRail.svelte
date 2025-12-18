<script lang="ts">
	/**
	 * ActivityRail - Vertical navigation rail for The Deck
	 *
	 * Displays mode buttons with active states, badges, and tooltips.
	 * Switches to horizontal layout on mobile.
	 */

	import { Zap, Monitor, Bot, Palette, FolderOpen, Settings } from 'lucide-svelte';
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

<div class="activity-rail" class:mobile={isMobile}>
	<!-- Logo button at top -->
	{#if !isMobile}
		<button
			class="logo-button"
			onclick={() => onLogoClick?.()}
			title="AI Hub"
		>
			<Zap size={24} strokeWidth={2} />
		</button>
	{/if}

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
					{:else if activity.mode === 'agents'}
						<Bot size={22} strokeWidth={1.5} />
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

	<!-- Settings button at bottom -->
	{#if !isMobile}
		<button
			class="settings-button"
			onclick={() => onSettingsClick?.()}
			title="Settings"
		>
			<Settings size={20} strokeWidth={1.5} />
			<span class="tooltip">Settings</span>
		</button>
	{/if}
</div>

<style>
	.activity-rail {
		display: flex;
		flex-direction: column;
		align-items: center;
		width: 64px;
		height: 100%;
		background: rgba(17, 17, 17, 0.95);
		border-right: 1px solid rgba(255, 255, 255, 0.08);
		padding: 12px 0;
		gap: 8px;
	}

	.activity-rail.mobile {
		flex-direction: row;
		width: 100%;
		height: 64px;
		border-right: none;
		border-top: 1px solid rgba(255, 255, 255, 0.08);
		padding: 0 12px;
		justify-content: space-around;
	}

	.logo-button {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		background: linear-gradient(135deg, #f59e0b 0%, #fb923c 100%);
		border: none;
		border-radius: 12px;
		color: white;
		cursor: pointer;
		transition: all 0.2s ease;
		margin-bottom: 16px;
	}

	.logo-button:hover {
		transform: scale(1.05);
		box-shadow: 0 0 20px rgba(251, 146, 60, 0.4);
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
		gap: 8px;
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
		color: rgba(255, 255, 255, 0.5);
		cursor: pointer;
		transition: all 0.2s ease;
	}

	.activity-button:hover {
		color: rgba(255, 255, 255, 0.9);
		background: rgba(255, 255, 255, 0.05);
	}

	.activity-button.active {
		color: var(--activity-color);
		background: color-mix(in srgb, var(--activity-color) 15%, transparent);
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
		box-shadow: 0 0 12px var(--activity-color);
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
		background: #ef4444;
		border-radius: 8px;
		font-size: 0.625rem;
		font-weight: 600;
		color: white;
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
		background: rgba(30, 30, 30, 0.95);
		border: 1px solid rgba(255, 255, 255, 0.1);
		border-radius: 6px;
		font-size: 0.75rem;
		color: white;
		white-space: nowrap;
		opacity: 0;
		visibility: hidden;
		transition: all 0.15s ease;
		pointer-events: none;
		z-index: 100;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
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
		color: rgba(255, 255, 255, 0.5);
		cursor: pointer;
		transition: all 0.2s ease;
		margin-top: auto;
	}

	.settings-button:hover {
		color: rgba(255, 255, 255, 0.9);
		background: rgba(255, 255, 255, 0.05);
	}
</style>
