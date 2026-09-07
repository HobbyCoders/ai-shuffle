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
	import { layout } from '$lib/stores/layout.svelte';

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

	// "No agents" and "none picked yet" look identical on the pill otherwise,
	// and only one of them is something the user can fix by opening the menu.
	const hasNoAgents = $derived($profiles.length === 0);
	const selectedProfileName = $derived(
		$profiles.find(p => p.id === $activeSession.profile)?.name
			|| (hasNoAgents ? 'No agents' : 'Agent')
	);

	function createAgent() {
		closeProfileDropdown();
		layout.openFeaturePage('profile');
	}

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
		class="ctx-chip context-selector-btn {$activeSession.profile ? '' : 'unset'}"
		class:open={showProfileDropdown}
		onclick={handleProfileClick}
		title="Change agent"
		aria-label="Agent: {selectedProfileName}"
		aria-haspopup="menu"
		aria-expanded={showProfileDropdown}
	>
		<svg class="selector-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.6" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
		</svg>
		<span class="ctx-chip-label selector-text">{selectedProfileName}</span>
		<svg class="ctx-chip-chev" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
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
				addLabel="Create agent"
				onAdd={createAgent}
				emptyMessage="No agents yet — create one to start chatting"
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
		flex-shrink: 0;
	}

	.selector-wrapper.sheet-mode:has(.selector-dropdown) {
		grid-column: 1 / -1;
	}

	.sheet-mode .context-selector-btn {
		width: 100%;
		height: 44px;
		padding: 0 12px;
		border-color: var(--border-subtle);
		border-radius: var(--composer-control-radius, 12px);
		background: var(--composer-control-bg, color-mix(in oklch, var(--muted) 55%, var(--card)));
	}

	.sheet-mode .selector-text {
		flex: 1;
		max-width: none;
		text-align: left;
		font-size: 0.8rem;
		font-weight: 600;
	}

	.sheet-mode .selector-dropdown {
		position: static;
		width: 100%;
		min-width: 0;
		max-width: none;
		/* The shared list owns touch scrolling. A second clipped scrollbar
		   around it made the lower agents unreachable on phones. */
		max-height: none;
		overflow: visible;
		--selector-scroll-height: min(180px, calc(var(--app-viewport-height, 100dvh) * 0.24));
		margin-top: 6px;
	}

	.sheet-mode :global(.menu-item) {
		min-height: 44px;
	}

	/* The agent chip is the first item in the composer's context row. It shares
	   the `.ctx-chip` look (app.css) and only differs from its neighbours by
	   carrying the foreground colour: the agent reads as the composer's subject. */
	.context-selector-btn {
		color: var(--foreground);
	}

	/* No agent picked (or none exist): amber so the missing choice is visible. */
	.context-selector-btn.unset {
		color: var(--warning);
	}

	.context-selector-btn.unset:hover {
		background: color-mix(in oklch, var(--warning) 12%, var(--muted));
		color: var(--warning);
	}

	.selector-icon {
		width: 12px;
		height: 12px;
	}

	.context-selector-btn.unset .selector-icon,
	.context-selector-btn.unset .ctx-chip-chev {
		color: var(--warning);
	}

	.selector-text {
		max-width: 80px;
	}

	/* Desktop shell: allow wider name */
	:global(html[data-platform='desktop']) .selector-text {
		max-width: 160px;
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

	/* Mobile shell layout adjustments */
	/* In the app the pill bar wraps onto multiple lines, so the profile pill is
	   free to grow — show the full profile name instead of truncating it.
	   Bound to the viewport (minus the bar's padding) so a pathologically long
	   name can't overflow horizontally; normal names show in full. */
	:global(html[data-platform='mobile']) .selector-text {
		font-size: 12px;
		max-width: calc(100vw - 4rem);
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* In the pill bar the dropdown opens upward; keep it within the viewport
	   by anchoring to the bar's full width. */
	:global(html[data-platform='mobile']) .selector-wrapper {
		position: static;
	}

	:global(html[data-platform='mobile']) .selector-dropdown {
		left: 12px;
		right: 12px;
		min-width: 0;
		max-width: none;
	}
</style>
