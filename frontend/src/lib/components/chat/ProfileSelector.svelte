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

	interface Props {
		sheetMode?: boolean;
	}

	let { sheetMode = false }: Props = $props();

	let profileDropdownRef = $state<HTMLDivElement | null>(null);
	let groupedSelector = $state<ReturnType<typeof GroupedSelector> | null>(null);

	// The sheet uses a distinct shared id: its CSS-hidden composer counterpart
	// remains mounted on phones, so a plain `profile` id would render two menus.
	// Distinct ids still preserve single-open coordination with the branch row.
	const dropdownId = $derived(sheetMode ? 'sheet-profile' as const : 'profile' as const);
	const showProfileDropdown = $derived($openHeaderDropdown === dropdownId);

	const selectedProfileName = $derived($profiles.find(p => p.id === $activeSession.profile)?.name || 'Agent');

	function handleProfileClick(e: MouseEvent) {
		e.stopPropagation();
		if (showProfileDropdown) {
			closeProfileDropdown();
		} else {
			openDropdown(dropdownId);
			groupedSelector?.reset();
		}
	}

	function closeProfileDropdown() {
		closeDropdown(dropdownId);
	}

	function selectProfile(profileId: string) {
		activeSession.setProfile(profileId);
		closeProfileDropdown();
	}

	function handleClickOutside(e: MouseEvent) {
		if (showProfileDropdown && profileDropdownRef && !profileDropdownRef.contains(e.target as Node)) {
			closeProfileDropdown();
		}
	}

	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			closeProfileDropdown();
		}
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		document.addEventListener('keydown', handleEscape);
		return () => {
			document.removeEventListener('click', handleClickOutside);
			document.removeEventListener('keydown', handleEscape);
			closeProfileDropdown();
		};
	});
</script>

<div class="selector-wrapper" class:sheet-mode={sheetMode} bind:this={profileDropdownRef}>
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

	.selector-wrapper.sheet-mode {
		position: relative;
		width: 100%;
		flex-shrink: 1;
	}

	.sheet-mode .context-selector-btn {
		width: 100%;
		height: 52px;
		padding: 0 14px;
		border-color: var(--border-subtle);
		border-radius: 14px;
		background: color-mix(in oklch, var(--muted) 55%, var(--card));
	}

	.sheet-mode .selector-text {
		flex: 1;
		max-width: none;
		text-align: left;
		font-size: 0.86rem;
		font-weight: 600;
	}

	.sheet-mode .selector-dropdown {
		position: static;
		width: 100%;
		min-width: 0;
		max-width: none;
		max-height: 45dvh;
		margin-top: 6px;
	}

	/* The mockup's `.pill.agent`: standard pill metrics with a primary-tinted
	   border and a teal glyph, so the agent reads as the composer's subject. */
	.context-selector-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 26px;
		padding: 0 9px;
		background: var(--scrim);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
		border-radius: 999px;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
		transition: border-color 0.13s ease, color 0.13s ease, background-color 0.13s ease;
		white-space: nowrap;
	}

	.context-selector-btn:hover {
		border-color: color-mix(in oklch, var(--primary) 55%, transparent);
		color: var(--foreground);
	}

	.context-selector-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
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
		color: var(--primary);
	}

	/* The unset state repaints the whole pill amber, glyph included. */
	.context-selector-btn.unset .selector-icon svg {
		color: var(--warning);
	}

	.selector-text {
		font-size: 12px;
		font-weight: 500;
		color: var(--foreground);
		line-height: 1;
		letter-spacing: 0.01em;
		max-width: 80px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.context-selector-btn.unset .selector-text {
		color: inherit;
	}

	/* Desktop: allow wider name */
	@media (min-width: 640px) {
		.selector-text {
			max-width: 140px;
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
			font-size: 12px;
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
		   (26px) so all context pills line up at the same height on mobile. */
		.context-selector-btn {
			height: 26px;
			padding: 0 9px;
			font-size: 12px;
		}
	}
</style>
