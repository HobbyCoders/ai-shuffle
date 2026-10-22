<script lang="ts">
	/**
	 * ProfileSelector - Profile selection dropdown in the controls dock.
	 * Uses GroupedSelector for group-aware profile selection.
	 *
	 * In the Assistant's own conversation the agent is fixed, so the chip turns
	 * into the way to the Assistant's settings: its avatar, its name and a gear.
	 * The conversation itself carries no header, so this chip (in the composer
	 * on desktop, in the controls sheet on the phone) is that door.
	 */
	import { onMount } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data.svelte';
	import GroupedSelector from '$lib/components/GroupedSelector.svelte';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';
	import { layout } from '$lib/stores/layout.svelte';
	import { assistantState } from '$lib/stores/assistant';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
	import { Settings2 } from 'lucide-svelte';

	interface Props {
		sheetMode?: boolean;
		/**
		 * `card`: the new-session agent card's name (#371 step 4). The agent's
		 * name in the display face with a chevron; the same menu opens below it.
		 */
		variant?: 'chip' | 'card';
	}

	let { sheetMode = false, variant = 'chip' }: Props = $props();

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

	const assistant = $derived(
		$isAdmin && $assistantState?.home_session_id && $assistantState.home_session_id === $activeSession.sessionId
			? $assistantState
			: null
	);
	const assistantName = $derived(assistant?.assistant_name?.trim() || 'Assistant');

	function openAssistantSettings() {
		if (assistant) layout.openFeaturePage('assistant', assistant.agent_id);
	}

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

<div class="selector-wrapper" class:sheet-mode={sheetMode} class:card-variant={variant === 'card'} bind:this={profileDropdownRef}>
	{#if variant === 'card' && !assistant}
	<button
		type="button"
		class="card-name {$activeSession.profile ? '' : 'unset'}"
		class:open={showProfileDropdown}
		onclick={handleProfileClick}
		title="Change agent"
		aria-label="Agent: {selectedProfileName}"
		aria-haspopup="menu"
		aria-expanded={showProfileDropdown}
	>
		<span class="card-name-text">{$activeSession.profile || hasNoAgents ? selectedProfileName : 'Pick an agent'}</span>
		<svg class="card-name-chev" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
		</svg>
	</button>
	{:else if assistant}
	<button
		type="button"
		class="ctx-chip context-selector-btn assistant-chip"
		onclick={openAssistantSettings}
		title="Assistant settings"
		aria-label="{assistantName} settings"
	>
		<AgentAvatar avatar={assistant.avatar_id} size={sheetMode ? 22 : 16} />
		<span class="ctx-chip-label selector-text">{assistantName}</span>
		<Settings2 class="assistant-gear" size={sheetMode ? 15 : 12} strokeWidth={2} aria-hidden="true" />
	</button>
	{:else}
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
	{/if}

	{#if showProfileDropdown && !assistant}
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

	/* The Assistant's chip: the avatar stands in for the person icon and a gear
	   says the tap opens settings rather than a list. */
	.assistant-chip {
		gap: 6px;
	}
	.assistant-chip :global(.agent-avatar) {
		border-radius: 30%;
	}
	.assistant-chip :global(.assistant-gear) {
		flex: none;
		color: var(--muted-foreground);
	}
	.assistant-chip:hover :global(.assistant-gear) {
		color: var(--foreground);
	}
	.sheet-mode .assistant-chip :global(.assistant-gear) {
		margin-left: auto;
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
	/* ===== CARD VARIANT (new-session agent card) ===== */
	.card-name {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		max-width: 100%;
		margin-left: -6px;
		padding: 0 6px;
		border: none;
		border-radius: calc(var(--radius, 10px) * 0.8);
		background: transparent;
		color: var(--foreground);
		font-family: var(--font-display, Georgia, serif);
		font-size: clamp(1.5rem, 3.2vw, 1.875rem);
		font-weight: 700;
		line-height: 1.15;
		letter-spacing: -0.02em;
		cursor: pointer;
		transition: background 150ms cubic-bezier(0.4, 0, 0.2, 1);
	}
	.card-name:hover, .card-name.open { background: var(--hover-overlay); }
	.card-name:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.card-name.unset { color: var(--warning); }
	.card-name-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.card-name-chev {
		flex: none;
		width: 16px;
		height: 16px;
		color: var(--subtle-foreground);
		transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.card-name.open .card-name-chev { transform: rotate(180deg); }
	/* The card sits above the composer: its menu opens downward. */
	.card-variant .selector-dropdown {
		bottom: auto;
		top: calc(100% + 6px);
		animation-name: dropdown-fade-down;
	}
	@keyframes dropdown-fade-down {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}
	@media (prefers-reduced-motion: reduce) {
		.card-name, .card-name-chev { transition: none; }
		.selector-dropdown { animation: none; }
	}

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

</style>
