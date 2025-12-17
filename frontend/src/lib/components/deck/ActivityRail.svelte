<script lang="ts">
	/**
	 * ActivityRail - Vertical icon navigation bar
	 *
	 * Features:
	 * - App logo at top
	 * - Activity icons for different modes (Chat, Agents, Studio, Files)
	 * - Settings icon at bottom
	 * - Active state with glowing indicator bar
	 * - Badge support for notifications
	 */

	import type { ActivityMode, ActivityBadges } from './types';

	interface Props {
		/** Currently active mode */
		activeMode: ActivityMode;
		/** Badge configuration for each mode */
		badges?: ActivityBadges;
		/** Called when mode changes */
		onModeChange?: (mode: ActivityMode) => void;
		/** Called when settings is clicked */
		onSettingsClick?: () => void;
	}

	let {
		activeMode = 'chat',
		badges = {},
		onModeChange,
		onSettingsClick
	}: Props = $props();

	// Activity items configuration
	const activities: Array<{ mode: ActivityMode; label: string; icon: string }> = [
		{
			mode: 'chat',
			label: 'Chat',
			icon: 'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z'
		},
		{
			mode: 'agents',
			label: 'Agents',
			icon: 'M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z'
		},
		{
			mode: 'studio',
			label: 'Studio',
			icon: 'M4.098 19.902a3.75 3.75 0 005.304 0l6.401-6.402M6.75 21A3.75 3.75 0 013 17.25V4.125C3 3.504 3.504 3 4.125 3h5.25c.621 0 1.125.504 1.125 1.125v4.072M6.75 21a3.75 3.75 0 003.75-3.75V8.197M6.75 21h13.125c.621 0 1.125-.504 1.125-1.125v-5.25c0-.621-.504-1.125-1.125-1.125h-4.072M10.5 8.197l2.88-2.88c.438-.439 1.15-.439 1.59 0l3.712 3.713c.44.44.44 1.152 0 1.59l-2.879 2.88M6.75 17.25h.008v.008H6.75v-.008z'
		},
		{
			mode: 'files',
			label: 'Files',
			icon: 'M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z'
		}
	];

	function handleModeChange(mode: ActivityMode) {
		onModeChange?.(mode);
	}

	function getBadgeVariantClass(variant?: string): string {
		switch (variant) {
			case 'warning':
				return 'bg-warning text-warning-foreground';
			case 'error':
				return 'bg-destructive text-destructive-foreground';
			case 'success':
				return 'bg-success text-success-foreground';
			default:
				return 'bg-primary text-primary-foreground';
		}
	}
</script>

<nav
	class="activity-rail flex flex-col items-center h-full bg-card/50 backdrop-blur-xl border-r border-border"
	aria-label="Main navigation"
>
	<!-- App Logo -->
	<div class="logo-container flex items-center justify-center w-full py-4">
		<button
			class="logo-button w-10 h-10 rounded-xl bg-gradient-to-br from-primary/20 to-primary/5 flex items-center justify-center hover:from-primary/30 hover:to-primary/10 transition-all duration-200"
			aria-label="AI Hub Home"
			onclick={() => handleModeChange('chat')}
		>
			<svg
				class="w-5 h-5 text-primary"
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
				aria-hidden="true"
			>
				<path
					stroke-linecap="round"
					stroke-linejoin="round"
					stroke-width="2"
					d="M13 10V3L4 14h7v7l9-11h-7z"
				/>
			</svg>
		</button>
	</div>

	<!-- Divider -->
	<div class="w-8 h-px bg-border mb-2"></div>

	<!-- Activity Icons -->
	<div class="activity-items flex-1 flex flex-col items-center gap-1 py-2 w-full">
		{#each activities as activity}
			{@const isActive = activeMode === activity.mode}
			{@const badge = badges[activity.mode]}
			<div class="relative w-full flex justify-center">
				<!-- Active indicator bar -->
				{#if isActive}
					<div
						class="active-indicator absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-8 rounded-r-full bg-primary shadow-glow"
						aria-hidden="true"
					></div>
				{/if}

				<button
					onclick={() => handleModeChange(activity.mode)}
					class="
						activity-button relative w-10 h-10 rounded-xl flex items-center justify-center
						transition-all duration-200 group
						{isActive
							? 'bg-primary/15 text-primary shadow-glow-soft'
							: 'text-muted-foreground hover:text-foreground hover:bg-accent'}
					"
					aria-label={activity.label}
					aria-current={isActive ? 'page' : undefined}
				>
					<svg
						class="w-5 h-5 transition-transform duration-200 group-hover:scale-110"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
						stroke-width="1.5"
						aria-hidden="true"
					>
						<path stroke-linecap="round" stroke-linejoin="round" d={activity.icon} />
					</svg>

					<!-- Badge -->
					{#if badge}
						<span
							class="
								absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] rounded-full
								flex items-center justify-center text-[10px] font-bold
								{getBadgeVariantClass(badge.variant)}
								{badge.dot ? 'min-w-[10px] h-[10px]' : 'px-1'}
							"
							aria-label="{badge.count || ''} notifications"
						>
							{#if !badge.dot && badge.count}
								{badge.count > 99 ? '99+' : badge.count}
							{/if}
						</span>
					{/if}

					<!-- Tooltip -->
					<span
						class="
							absolute left-full ml-3 px-2 py-1 rounded-md
							bg-popover text-popover-foreground text-xs font-medium
							opacity-0 invisible group-hover:opacity-100 group-hover:visible
							transition-all duration-200 whitespace-nowrap z-50
							shadow-lg border border-border
						"
						role="tooltip"
					>
						{activity.label}
					</span>
				</button>
			</div>
		{/each}
	</div>

	<!-- Bottom Section: Settings -->
	<div class="bottom-section flex flex-col items-center gap-1 py-4 w-full border-t border-border">
		<button
			onclick={() => onSettingsClick?.()}
			class="
				settings-button w-10 h-10 rounded-xl flex items-center justify-center
				text-muted-foreground hover:text-foreground hover:bg-accent
				transition-all duration-200 group relative
			"
			aria-label="Settings"
		>
			<svg
				class="w-5 h-5 transition-transform duration-200 group-hover:rotate-45"
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
				stroke-width="1.5"
				aria-hidden="true"
			>
				<path
					stroke-linecap="round"
					stroke-linejoin="round"
					d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z"
				/>
				<path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
			</svg>

			<!-- Tooltip -->
			<span
				class="
					absolute left-full ml-3 px-2 py-1 rounded-md
					bg-popover text-popover-foreground text-xs font-medium
					opacity-0 invisible group-hover:opacity-100 group-hover:visible
					transition-all duration-200 whitespace-nowrap z-50
					shadow-lg border border-border
				"
				role="tooltip"
			>
				Settings
			</span>
		</button>
	</div>
</nav>

<style>
	.activity-rail {
		width: 64px;
	}

	.shadow-glow {
		box-shadow: 0 0 12px var(--glow-color), 0 0 4px var(--glow-color-soft);
	}

	.shadow-glow-soft {
		box-shadow: 0 0 8px var(--glow-color-soft);
	}

	/* Active indicator animation */
	.active-indicator {
		animation: indicator-slide-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes indicator-slide-in {
		from {
			opacity: 0;
			transform: translateY(-50%) scaleY(0);
		}
		to {
			opacity: 1;
			transform: translateY(-50%) scaleY(1);
		}
	}

	/* Mobile: Rail becomes bottom nav */
	@media (max-width: 640px) {
		.activity-rail {
			width: 100%;
			height: 64px;
			flex-direction: row;
			border-right: none;
			border-top: 1px solid var(--border);
		}

		.logo-container {
			display: none;
		}

		.activity-items {
			flex-direction: row;
			justify-content: center;
			gap: 0.5rem;
			padding: 0.5rem;
		}

		.activity-items > div {
			width: auto;
		}

		.active-indicator {
			left: 50%;
			top: 0;
			transform: translateX(-50%);
			width: 2rem;
			height: 3px;
			border-radius: 0 0 9999px 9999px;
		}

		@keyframes indicator-slide-in {
			from {
				opacity: 0;
				transform: translateX(-50%) scaleX(0);
			}
			to {
				opacity: 1;
				transform: translateX(-50%) scaleX(1);
			}
		}

		.bottom-section {
			flex-direction: row;
			border-top: none;
			border-left: 1px solid var(--border);
			padding: 0.5rem;
		}

		/* Hide tooltips on mobile */
		.activity-button span[role='tooltip'],
		.settings-button span[role='tooltip'] {
			display: none;
		}
	}
</style>
