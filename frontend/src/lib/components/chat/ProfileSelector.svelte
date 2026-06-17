<script lang="ts">
	/**
	 * ProfileSelector - Profile selection dropdown in the controls dock.
	 * Uses GroupedSelector for group-aware profile selection.
	 */
	import { onMount } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data';
	import GroupedSelector from '$lib/components/GroupedSelector.svelte';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';

	let profileDropdownRef = $state<HTMLDivElement | null>(null);
	let groupedSelector = $state<ReturnType<typeof GroupedSelector> | null>(null);

	// Driven by the shared store so opening this closes the branch/status dropdowns.
	const showProfileDropdown = $derived($openHeaderDropdown === 'profile');

	const selectedProfileName = $derived($profiles.find(p => p.id === $activeSession.profile)?.name || 'Profile');

	function handleProfileClick(e: MouseEvent) {
		e.stopPropagation();
		if (showProfileDropdown) {
			closeDropdown('profile');
		} else {
			openDropdown('profile');
			groupedSelector?.reset();
		}
	}

	function selectProfile(profileId: string) {
		activeSession.setProfile(profileId);
		closeDropdown('profile');
	}

	function handleClickOutside(e: MouseEvent) {
		if (showProfileDropdown && profileDropdownRef && !profileDropdownRef.contains(e.target as Node)) {
			closeDropdown('profile');
		}
	}

	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			closeDropdown('profile');
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		document.addEventListener('keydown', handleEscape);
		return () => {
			document.removeEventListener('click', handleClickOutside);
			document.removeEventListener('keydown', handleEscape);
			closeDropdown('profile');
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
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
			</svg>
		</div>
		<span class="selector-text">{selectedProfileName}</span>
		<svg class="chevron" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M19 9l-7 7-7-7" />
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
		flex-shrink: 0;
	}

	.context-selector-btn {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 26px;
		padding: 0 8px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.context-selector-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.context-selector-btn.unset {
		border-color: var(--warning);
		color: var(--warning);
	}

	.context-selector-btn.unset:hover {
		background: color-mix(in oklch, var(--warning) 10%, transparent);
		color: var(--warning);
	}

	.selector-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
	}

	.selector-icon svg {
		color: currentColor;
	}

	.selector-text {
		font-size: 0.6875rem;
		font-weight: 500;
		color: inherit;
		line-height: 1;
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

	/* Chevron — sized to match the git buttons' ChevronDown (11px), not animated */
	.chevron {
		width: 11px;
		height: 11px;
		flex-shrink: 0;
		color: inherit;
		opacity: 0.6;
	}

	/* ===== DROPDOWN MENU ===== */
	/* Lives on the left of the header rail, so it left-aligns under the button
	   and expands rightward (centering would push it off the panel's left edge). */
	.selector-dropdown {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		min-width: 200px;
		max-width: 280px;
		max-height: 60vh;
		overflow-y: auto;
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
			transform: translateY(-4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
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
		/* Match git buttons' mobile text size */
		.selector-text {
			font-size: 0.75rem;
		}

		/* Header is a horizontal scroll rail on mobile; pin the dropdown as a fixed
		   panel below the header so it isn't clipped by the rail's overflow. */
		.selector-dropdown {
			position: fixed;
			top: 92px;
			left: 8px;
			right: 8px;
			bottom: auto;
			transform: none;
			min-width: 0;
			max-width: none;
			max-height: 60vh;
			z-index: 200;
		}

		@keyframes dropdown-fade-in {
			from { opacity: 0; transform: translateY(-4px); }
			to { opacity: 1; transform: translateY(0); }
		}

		/* Match git buttons' mobile touch target */
		.context-selector-btn {
			height: 36px;
			padding: 0 12px;
			font-size: 0.75rem;
		}
	}
</style>
