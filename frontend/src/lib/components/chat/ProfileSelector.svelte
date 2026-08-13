<script lang="ts">
	/**
	 * ProfileSelector - Profile selection dropdown in the controls dock.
	 * Uses GroupedSelector for group-aware profile selection.
	 */
	import { onMount } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data.svelte';
	import GroupedSelector from '$lib/components/GroupedSelector.svelte';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';

	let profileDropdownRef = $state<HTMLDivElement | null>(null);
	let groupedSelector = $state<ReturnType<typeof GroupedSelector> | null>(null);

	// Driven by the shared store so opening this closes the branch/status dropdowns.
	const showProfileDropdown = $derived($openHeaderDropdown === 'profile');

	const selectedProfileName = $derived($profiles.find(p => p.id === $activeSession.profile)?.name || 'Agent');

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
		title="Change agent"
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
		<div class="selector-dropdown" role="menu" aria-label="Agent selection">
			<GroupedSelector
				bind:this={groupedSelector}
				items={$profiles}
				entityType="profiles"
				selectedId={$activeSession.profile}
				onSelect={selectProfile}
				emptyMessage="No agents available"
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
		height: 28px;
		padding: 0 10px;
		background: color-mix(in oklch, var(--foreground) 6%, var(--background));
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		color: var(--muted-foreground);
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.context-selector-btn:hover {
		border-color: var(--primary);
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
		font-size: 0.75rem;
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
	/* Lives in the context pill bar just above the chat input, so it opens
	   UPWARD (anchored to the pill's bottom edge) and left-aligns under the
	   button. */
	.selector-dropdown {
		position: absolute;
		bottom: calc(100% + 4px);
		left: 0;
		min-width: 200px;
		max-width: min(280px, calc(100vw - 24px));
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
			transform: translateY(4px);
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
		/* On mobile the pill bar wraps onto multiple lines, so the profile pill is
		   free to grow — show the full profile name instead of truncating it.
		   Bound to the viewport (minus the bar's padding) so a pathologically long
		   name can't overflow horizontally; normal names show in full. */
		.selector-text {
			font-size: 0.75rem;
			max-width: calc(100vw - 4rem);
			overflow: hidden;
			text-overflow: ellipsis;
		}

		/* In the pill bar the dropdown opens upward; keep it within the viewport
		   by anchoring to the bar's full width. */
		.selector-wrapper {
			position: static;
		}

		.selector-dropdown {
			left: 12px;
			right: 12px;
			min-width: 0;
			max-width: none;
		}

		/* Keep the same pill metrics as the branch selector / worktree badge
		   (28px) so all context pills line up at the same height on mobile. */
		.context-selector-btn {
			height: 28px;
			padding: 0 10px;
			font-size: 0.75rem;
		}
	}
</style>
