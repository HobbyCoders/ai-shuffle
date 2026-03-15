<script lang="ts">
	/**
	 * ProfileSelector - Profile selection dropdown in the controls dock.
	 * Uses GroupedSelector for group-aware profile selection.
	 */
	import { onMount } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data';
	import GroupedSelector from '$lib/components/GroupedSelector.svelte';

	let showProfileDropdown = $state(false);
	let profileDropdownRef = $state<HTMLDivElement | null>(null);
	let groupedSelector = $state<ReturnType<typeof GroupedSelector> | null>(null);

	const selectedProfileName = $derived($profiles.find(p => p.id === $activeSession.profile)?.name || 'Profile');

	function handleProfileClick(e: MouseEvent) {
		e.stopPropagation();
		showProfileDropdown = !showProfileDropdown;
		if (showProfileDropdown) {
			groupedSelector?.reset();
		}
	}

	function selectProfile(profileId: string) {
		activeSession.setProfile(profileId);
		showProfileDropdown = false;
	}

	function handleClickOutside(e: MouseEvent) {
		if (profileDropdownRef && !profileDropdownRef.contains(e.target as Node)) {
			showProfileDropdown = false;
		}
	}

	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			showProfileDropdown = false;
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		document.addEventListener('keydown', handleEscape);
		return () => {
			document.removeEventListener('click', handleClickOutside);
			document.removeEventListener('keydown', handleEscape);
		};
	});
</script>

<div class="selector-wrapper" bind:this={profileDropdownRef}>
	<button
		type="button"
		class="context-selector-btn {$activeSession.profile ? '' : 'unset'}"
		onclick={handleProfileClick}
		title="Change profile"
		aria-haspopup="menu"
		aria-expanded={showProfileDropdown}
	>
		<div class="selector-icon">
			<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
			</svg>
		</div>
		<span class="selector-text">{selectedProfileName}</span>
		<svg class="w-3 h-3 opacity-50 chevron" class:open={showProfileDropdown} fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
		</svg>
	</button>

	{#if showProfileDropdown}
		<div class="selector-dropdown" role="menu" aria-label="Profile selection">
			<GroupedSelector
				bind:this={groupedSelector}
				items={$profiles}
				entityType="profiles"
				selectedId={$activeSession.profile}
				onSelect={selectProfile}
				emptyMessage="No profiles available"
			/>
		</div>
	{/if}
</div>

<style>
	/* ===== SELECTORS (Profile) ===== */
	.selector-wrapper {
		position: relative;
	}

	.context-selector-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px 4px 8px;
		background: transparent;
		border: 1px solid transparent;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.context-selector-btn:hover {
		background: var(--hover-overlay);
		border-color: var(--border-subtle);
	}

	.context-selector-btn.unset {
		color: var(--warning);
	}

	.context-selector-btn.unset:hover {
		background: var(--status-warning-bg);
		border-color: var(--status-warning-border);
	}

	.selector-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: calc(var(--radius, 0.75rem) * 0.42);
		background: var(--muted);
		border: 1px solid var(--border-subtle);
	}

	.selector-icon svg {
		color: var(--muted-foreground);
	}

	.selector-text {
		font-size: 12px;
		font-weight: 500;
		color: var(--muted-foreground);
		letter-spacing: 0.01em;
		max-width: 80px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Desktop: allow wider name */
	@media (min-width: 640px) {
		.selector-text {
			max-width: 120px;
		}
	}

	/* Chevron */
	.chevron {
		color: var(--muted-foreground);
		transition: transform 0.2s ease;
	}

	.chevron.open {
		transform: rotate(180deg);
	}

	/* ===== DROPDOWN MENU ===== */
	.selector-dropdown {
		position: absolute;
		bottom: calc(100% + 4px);
		left: 50%;
		transform: translateX(-50%);
		min-width: 200px;
		max-width: 280px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		z-index: 50;
		padding: 6px;
		animation: dropdown-fade-in 0.15s ease;
	}

	@keyframes dropdown-fade-in {
		from {
			opacity: 0;
			transform: translateX(-50%) translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateX(-50%) translateY(0);
		}
	}

	/* Scrollbar styling for dropdown */
	.selector-dropdown::-webkit-scrollbar {
		width: 6px;
	}

	.selector-dropdown::-webkit-scrollbar-track {
		background: transparent;
	}

	.selector-dropdown::-webkit-scrollbar-thumb {
		background: var(--scrollbar-thumb);
		border-radius: calc(var(--radius, 0.75rem) * 0.25);
	}

	.selector-dropdown::-webkit-scrollbar-thumb:hover {
		background: var(--scrollbar-thumb-hover);
	}

	/* Responsive: mobile layout adjustments */
	@media (max-width: 639px) {
		/* Slightly smaller text on mobile */
		.selector-text {
			font-size: 11px;
		}

		/* Make selectors more compact */
		.context-selector-btn {
			padding: 4px 6px 4px 4px;
			gap: 4px;
		}

		/* Smaller selector icon */
		.selector-icon {
			width: 18px;
			height: 18px;
		}

		/* Hide chevron on mobile to save space */
		.context-selector-btn .chevron {
			display: none;
		}
	}
</style>
