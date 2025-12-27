<script lang="ts">
	/**
	 * ActivityRail - Vertical navigation rail for The Deck
	 *
	 * Displays mode buttons with active states, badges, and tooltips.
	 * Switches to horizontal layout on mobile.
	 */

	import { Plus, Monitor, Palette, FolderOpen, Settings, LogOut } from 'lucide-svelte';
	import { auth } from '$lib/stores/auth';
	import type { ActivityMode, ActivityBadges } from './types';

	interface Props {
		activeMode: ActivityMode;
		badges?: ActivityBadges;
		isMobile?: boolean;
		isAdmin?: boolean;
		onModeChange?: (mode: ActivityMode) => void;
		onLogoClick?: () => void;
		onSettingsClick?: () => void;
	}

	let {
		activeMode,
		badges = {},
		isMobile = false,
		isAdmin = false,
		onModeChange,
		onLogoClick,
		onSettingsClick
	}: Props = $props();

	let loggingOut = $state(false);

	async function handleLogout() {
		if (loggingOut) return;
		loggingOut = true;
		try {
			await auth.logout();
		} catch (e) {
			console.error('Logout failed:', e);
		}
		loggingOut = false;
	}

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
	<!-- Dealer Button - Casino style plus button -->
	<button
		class="dealer-button"
		onclick={() => onLogoClick?.()}
		title="Deal New Card"
	>
		<div class="dealer-button-disc">
			<Plus size={20} strokeWidth={2.5} class="dealer-icon" />
		</div>
		<span class="tooltip">Deal New Card</span>
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

	<!-- Bottom button - admin sees Settings, non-admin sees Logout -->
	{#if isAdmin}
		<button
			class="settings-button"
			onclick={() => onSettingsClick?.()}
			title="Settings"
		>
			<Settings size={20} strokeWidth={1.5} />
			<span class="tooltip">Settings</span>
		</button>
	{:else}
		<button
			class="settings-button logout-button"
			onclick={handleLogout}
			disabled={loggingOut}
			title="Logout"
		>
			<LogOut size={20} strokeWidth={1.5} />
			<span class="tooltip">{loggingOut ? 'Logging out...' : 'Logout'}</span>
		</button>
	{/if}
</div>

<style>
	.activity-rail {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		width: 76px;
		height: 100%;
		background: linear-gradient(180deg,
			var(--felt-deep) 0%,
			var(--felt) 15%,
			var(--felt) 85%,
			var(--felt-deep) 100%
		);
		border-right: 1px solid color-mix(in srgb, var(--border) 25%, transparent);
		padding: 16px 0;
		gap: 8px;
		box-shadow:
			2px 0 20px rgba(0, 0, 0, 0.3),
			inset -1px 0 0 color-mix(in srgb, var(--border) 25%, transparent);
	}

	/* Art deco ornament - top */
	.activity-rail::before {
		content: '';
		position: absolute;
		top: 12px;
		left: 50%;
		transform: translateX(-50%);
		width: 40px;
		height: 1px;
		background: linear-gradient(90deg, transparent, var(--gold-dim), transparent);
		opacity: 0.5;
	}

	/* Art deco ornament - bottom */
	.activity-rail::after {
		content: '';
		position: absolute;
		bottom: 12px;
		left: 50%;
		transform: translateX(-50%);
		width: 40px;
		height: 1px;
		background: linear-gradient(90deg, transparent, var(--gold-dim), transparent);
		opacity: 0.5;
	}

	.activity-rail.mobile {
		flex-direction: row;
		width: 100%;
		height: auto;
		min-height: 64px;
		border-right: none;
		border-top: 1px solid color-mix(in srgb, var(--border) 40%, transparent);
		background: color-mix(in srgb, var(--felt) 95%, transparent);
		backdrop-filter: blur(16px);
		-webkit-backdrop-filter: blur(16px);
		padding: 8px 16px;
		padding-bottom: max(12px, env(safe-area-inset-bottom, 12px));
		padding-left: max(16px, env(safe-area-inset-left, 16px));
		padding-right: max(16px, env(safe-area-inset-right, 16px));
		justify-content: space-around;
		box-shadow: 0 -2px 20px rgba(0, 0, 0, 0.3);
	}

	.activity-rail.mobile::before,
	.activity-rail.mobile::after {
		display: none;
	}

	/* ===================================
	   DEALER BUTTON - Casino chip style
	   =================================== */

	.dealer-button {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 52px;
		height: 52px;
		margin-bottom: 20px;
		cursor: pointer;
		border: none;
		background: transparent;
		-webkit-tap-highlight-color: transparent;
	}

	.dealer-button-disc {
		position: relative;
		width: 48px;
		height: 48px;
		display: flex;
		align-items: center;
		justify-content: center;

		/* Classic dealer button - white/cream plastic look */
		background:
			radial-gradient(ellipse 80% 50% at 50% 20%,
				rgba(255, 255, 255, 0.9) 0%,
				transparent 60%
			),
			linear-gradient(180deg,
				var(--dealer-white) 0%,
				var(--dealer-cream) 50%,
				var(--dealer-shadow) 100%
			);

		border-radius: 50%;

		/* Embossed edge effect */
		box-shadow:
			inset 0 2px 4px rgba(255, 255, 255, 0.8),
			inset 0 -3px 6px rgba(0, 0, 0, 0.15),
			0 4px 12px rgba(0, 0, 0, 0.4),
			0 2px 4px rgba(0, 0, 0, 0.3);

		transition: all 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
	}

	/* Gold ring border */
	.dealer-button-disc::before {
		content: '';
		position: absolute;
		inset: 4px;
		border: 2px solid var(--gold);
		border-radius: 50%;
		opacity: 0.6;
		transition: opacity 0.2s ease;
	}

	.dealer-button:hover .dealer-button-disc {
		transform: scale(1.08) translateY(-2px);
		box-shadow:
			inset 0 2px 4px rgba(255, 255, 255, 0.9),
			inset 0 -3px 6px rgba(0, 0, 0, 0.1),
			0 8px 20px rgba(0, 0, 0, 0.5),
			0 4px 8px rgba(0, 0, 0, 0.3),
			0 0 20px var(--gold-glow);
	}

	.dealer-button:hover .dealer-button-disc::before {
		opacity: 0.9;
	}

	.dealer-button:active .dealer-button-disc {
		transform: scale(0.95);
		transition-duration: 0.1s;
	}

	.dealer-button :global(.dealer-icon) {
		color: var(--dealer-text);
		filter: drop-shadow(0 1px 0 rgba(255, 255, 255, 0.5));
		transition: transform 0.25s ease;
	}

	.dealer-button:hover :global(.dealer-icon) {
		transform: rotate(90deg);
	}

	.dealer-button .tooltip {
		position: absolute;
		left: calc(100% + 14px);
		top: 50%;
		transform: translateY(-50%) translateX(-4px);
		padding: 8px 14px;
		background: color-mix(in srgb, var(--felt) 92%, transparent);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border: 1px solid var(--border);
		border-radius: 8px;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		opacity: 0;
		visibility: hidden;
		transition: all 0.2s ease;
		pointer-events: none;
		z-index: 100;
		box-shadow:
			inset 3px 0 0 var(--gold),
			0 4px 20px rgba(0, 0, 0, 0.3);
	}

	.dealer-button:hover .tooltip {
		opacity: 1;
		visibility: visible;
		transform: translateY(-50%) translateX(0);
	}

	/* Mobile dealer button */
	.mobile .dealer-button {
		margin-bottom: 0;
		width: 48px;
		height: 48px;
	}

	.mobile .dealer-button-disc {
		width: 44px;
		height: 44px;
	}

	.mobile .dealer-button .tooltip {
		left: 50%;
		top: auto;
		bottom: calc(100% + 12px);
		transform: translateX(-50%) translateY(4px);
	}

	.mobile .dealer-button:hover .tooltip {
		transform: translateX(-50%) translateY(0);
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
	.mobile .dealer-button:hover .tooltip {
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

	.settings-button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Logout button hover state - subtle red tint */
	.logout-button:hover {
		color: var(--destructive, #ef4444);
		background: color-mix(in srgb, var(--destructive, #ef4444) 10%, transparent);
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
		.mobile .dealer-button {
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

		.mobile .dealer-button,
		.mobile .activity-button,
		.mobile .settings-button {
			width: 44px;
			height: 44px;
			min-width: 44px;
			min-height: 44px;
		}
	}
</style>
