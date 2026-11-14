<script lang="ts">
	/**
	 * WorkspaceSelector - "Let's build [workspace]" empty state
	 *
	 * Codex-style workspace picker shown when starting a new chat.
	 * Centered hero with clickable workspace name dropdown.
	 * Uses GroupedSelector for group-aware project selection.
	 */
	import { tick } from 'svelte';
	import { ChevronDown, FolderClosed, FolderX } from 'lucide-svelte';
	import { floatingMenu } from '$lib/components/ui/floating-menu';
	import { menuHost as hostMenu } from '$lib/components/ui/menu-host';
	import { closeDropdown, openDropdown, openHeaderDropdown } from '$lib/stores/header-dropdowns';
	import { isMobileUi } from '$lib/stores/platform.svelte';
	import { appData, clusterProjects, profiles, profilesLoaded, projects, sessions } from '$lib/stores/app-data.svelte';
	import {
		PROJECT_PICKER_ATTRIBUTE,
		projectPickerAriaShortcut,
		projectPickerShortcutHint
	} from '$lib/services/project-picker';
	import AddProjectModal from '$lib/components/AddProjectModal.svelte';
	import GroupedSelector from '$lib/components/GroupedSelector.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import {
		buildWorkspacePickerItems,
		pickerItemName,
		projectRecency,
		rankPickerItems,
		selectedPickerId
	} from './workspace-catalog';

	interface Props {
		selectedProject: string | null;
		selectedProjectV2Id?: string | null;
		onSelectProject: (projectId: string, projectV2Id?: string | null) => void;
		/**
		 * `sentence`: the new-session row's "No project" control. The same
		 * picker drops down centred under it; picking one adds the project.
		 * Outside the phone sheet its list has a search field, most recently
		 * used projects first, and opens from Cmd/Ctrl+P too (`openPicker`).
		 */
		variant?: 'hero' | 'sentence';
		/**
		 * No trigger of its own: the sentence picker opened by `openPicker` on
		 * another control (the project word, to switch a picked project).
		 */
		headless?: boolean;
		/** Offers a "No project" row while a project is picked. */
		onClearProject?: () => void;
		/** The phone sheet's stacked row, with its list on the sheet's own page. */
		sheetMode?: boolean;
		menuHost?: HTMLElement | null;
	}

	let { selectedProject, selectedProjectV2Id = null, onSelectProject, variant = 'hero', headless = false, onClearProject, sheetMode = false, menuHost = null }: Props = $props();

	let showDropdown = $state(false);
	let dropdownRef = $state<HTMLElement | null>(null);
	/** The open list. The sentence variant floats it to <body>, outside dropdownRef. */
	let listRef = $state<HTMLDivElement | null>(null);
	let sentenceTrigger = $state<HTMLButtonElement | null>(null);
	let groupedSelector = $state<ReturnType<typeof GroupedSelector> | null>(null);
	const pickerId = $props.id();
	/** Opened by `openPicker` on this control instead of the trigger. */
	let anchorOverride = $state<HTMLElement | null>(null);
	/** Where focus goes back after a keyboard-opened pick or Escape (the message editor). */
	/** Where focus goes after a pick or Escape: an element, or a function that puts it back. */
	let returnTo: HTMLElement | (() => void) | null = null;
	let autofocusSearch = $state(false);
	const NO_PROJECT_ID = '__no_project__';
	// The desktop row's list: the phone sheet keeps its plain full-page list.
	const searchable = $derived(variant === 'sentence' && !sheetMode);
	/** Its id among the one-open-at-a-time menus: opening it closes the others, and theirs closes it. */
	const dropdownId = $derived(sheetMode ? 'sheet-new-project' as const : searchable ? 'new-project' as const : null);
	const shortcutHint = projectPickerShortcutHint();
	const ariaShortcut = projectPickerAriaShortcut();

	// On the mobile shell use the top layer so neither the chat surface nor the
	// keyboard can clip this popup. The desktop shell keeps its anchored dropdown
	// at every width.
	function mobilePopup(node: HTMLDivElement) {
		if (sheetMode) return;
		const viewport = window.visualViewport;
		const scrollport = dropdownRef?.closest<HTMLElement>('.chat-view');
		let topLayer = false;
		function position() {
			if (!isMobileUi()) {
				if (topLayer) node.hidePopover();
				topLayer = false;
				node.removeAttribute('popover');
				node.classList.remove('mobile-popup');
				for (const property of ['--selector-scroll-height', '--picker-left', '--picker-top']) node.style.removeProperty(property);
				return;
			}
			node.classList.add('mobile-popup');
			if (!topLayer && typeof node.showPopover === 'function') {
				node.setAttribute('popover', 'manual');
				node.showPopover();
				topLayer = true;
			}
			const anchor = dropdownRef?.getBoundingClientRect();
			if (!anchor) return;
			const bounds = scrollport?.getBoundingClientRect();
			if (bounds && (anchor.bottom <= bounds.top || anchor.top >= bounds.bottom)) {
				showDropdown = false;
				return;
			}
			const edge = 12;
			const viewTop = viewport?.offsetTop ?? 0;
			const viewBottom = viewTop + (viewport?.height ?? window.innerHeight);
			const below = viewBottom - anchor.bottom - edge - 8;
			const above = anchor.top - viewTop - edge - 8;
			const upwards = below < 180 && above > below;
			const available = upwards ? above : below;
			// Header, divider, footer and padding use at most 100px; only the
			// shared list scrolls. Recompute as the visual viewport changes.
			node.style.setProperty('--selector-scroll-height', `${Math.min(320, Math.max(44, available - 100))}px`);
			const rect = node.getBoundingClientRect();
			const left = Math.max(edge, Math.min(window.innerWidth - rect.width - edge, anchor.left + (anchor.width - rect.width) / 2));
			const top = Math.max(viewTop + edge, Math.min(viewBottom - rect.height - edge, upwards ? anchor.top - rect.height - 8 : anchor.bottom + 8));
			node.style.setProperty('--picker-left', `${left}px`);
			node.style.setProperty('--picker-top', `${top}px`);
		}
		const observer = new ResizeObserver(position);
		observer.observe(node);
		const hero = dropdownRef?.closest('.empty-hero');
		if (hero) observer.observe(hero);
		scrollport?.addEventListener('scroll', position);
		window.addEventListener('resize', position);
		viewport?.addEventListener('resize', position);
		viewport?.addEventListener('scroll', position);
		position();
		return { destroy() {
			observer.disconnect();
			scrollport?.removeEventListener('scroll', position);
			window.removeEventListener('resize', position);
			viewport?.removeEventListener('resize', position);
			viewport?.removeEventListener('scroll', position);
		} };
	}

	// Through the shared coordinator: another menu opening closes this list (and
	// in the sheet, its page's Back does).
	$effect(() => {
		if (dropdownId && showDropdown && $openHeaderDropdown !== dropdownId) showDropdown = false;
	});
	$effect(() => {
		if (dropdownId && !showDropdown) closeDropdown(dropdownId);
	});

	function closeOnEscape(event: KeyboardEvent) {
		if (event.key !== 'Escape' || !showDropdown) return;
		event.preventDefault();
		showDropdown = false;
		const back = takeReturnFocus();
		if (back) back();
		else (anchorOverride ?? dropdownRef?.querySelector('button'))?.focus({ preventScroll: true });
	}

	function takeReturnFocus(): (() => void) | null {
		const back = returnTo;
		returnTo = null;
		if (typeof back === 'function') return back;
		return back?.isConnected ? () => back.focus({ preventScroll: true }) : null;
	}

	// Add project modal state
	let showAddModal = $state(false);

	const pickerItems = $derived(buildWorkspacePickerItems($projects, $clusterProjects));
	const selectedId = $derived(
		selectedPickerId(selectedProject, selectedProjectV2Id, pickerItems)
	);
	// Most recently used first: the newest session per project in the sidebar's list.
	const recency = $derived(searchable ? projectRecency(pickerItems, $sessions) : new Map<string, number>());
	const rankProjects = (list: { id: string; name: string }[], query: string) =>
		rankPickerItems(list, query, recency);
	const noProjectRow = $derived(searchable && selectedId && onClearProject ? { id: NO_PROJECT_ID, name: 'No project' } : null);
	const resolvedProjectName = $derived(
		pickerItemName(pickerItems, selectedId) ||
			$projects.find((p) => p.id === selectedProject)?.name ||
			''
	);
	const selectedProjectName = $derived(resolvedProjectName || 'your project');
	// The visible text is only the project name; say what the button does.
	const triggerLabel = $derived(
		resolvedProjectName ? `Switch project, current: ${resolvedProjectName}` : 'Choose a project'
	);

	// Close dropdown on outside click
	function handleClickOutside(e: MouseEvent) {
		const target = e.target as Node;
		if (dropdownRef && !dropdownRef.contains(target) && !listRef?.contains(target)) {
			showDropdown = false;
			returnTo = null;
		}
	}

	function handleSelect(id: string) {
		const back = takeReturnFocus();
		showDropdown = false;
		if (id === NO_PROJECT_ID) {
			onClearProject?.();
		} else if (id !== selectedId || !searchable) {
			const item = pickerItems.find((i) => i.id === id);
			if (item) {
				onSelectProject(item.project, item.projectV2Id);
			} else {
				onSelectProject(id);
			}
		}
		// Opened from the message editor: keep typing there, once the pick has
		// re-rendered the row (a microtask: no key can land in between).
		if (back) void tick().then(back);
	}

	function openAddModal() {
		showDropdown = false;
		returnTo = null;
		showAddModal = true;
	}

	/**
	 * Open the searchable list from the keyboard (Cmd/Ctrl+P), anchored on
	 * `anchor` (default: this control), with the cursor in its search field.
	 * Picking or Escape puts focus back on `returnFocus`. Pressed again while
	 * the list is open (the cursor is in its search), it only refocuses the
	 * search and keeps the place focus goes back to.
	 */
	export function openPicker({ anchor = null, returnFocus = null }: { anchor?: HTMLElement | null; returnFocus?: HTMLElement | (() => void) | null } = {}) {
		if (showDropdown) {
			groupedSelector?.focusSearch();
			return;
		}
		returnTo = returnFocus;
		anchorOverride = anchor;
		autofocusSearch = true;
		showDropdown = true;
		if (dropdownId) openDropdown(dropdownId);
		groupedSelector?.reset();
		void appData.loadClusterProjects();
	}

	function toggleDropdown() {
		showDropdown = !showDropdown;
		returnTo = null;
		anchorOverride = null;
		// A mouse click types next only with a real keyboard at hand; a touch
		// screen would raise its keyboard over the list.
		autofocusSearch = typeof matchMedia === 'function' && matchMedia('(pointer: fine)').matches;
		if (showDropdown) {
			if (dropdownId) openDropdown(dropdownId);
			// Reset grouped selector to main panel when opening
			groupedSelector?.reset();
			void appData.loadClusterProjects();
		}
	}
</script>

<svelte:window onclick={handleClickOutside} onkeydown={closeOnEscape} />

{#if variant === 'sentence'}
<span class="picker-container sentence-variant" bind:this={dropdownRef}>
	{#if !headless}
	<button
		bind:this={sentenceTrigger}
		type="button"
		data-project-control
		class={sheetMode ? 'ctx-chip repo-chip' : 'sentence-tok'}
		aria-expanded={showDropdown}
		aria-haspopup="dialog"
		aria-controls={pickerId}
		aria-label="No project. Add a project (optional: adds checkout, branch and git)"
		aria-keyshortcuts={searchable ? ariaShortcut : undefined}
		title={searchable ? `Add a project (optional) · ${shortcutHint}` : 'Add a project (optional)'}
		onclick={toggleDropdown}
	><FolderClosed size={sheetMode ? 16 : 14} strokeWidth={1.75} aria-hidden="true" /><span class={sheetMode ? 'ctx-chip-label' : 'sentence-tok-label'}>No project</span>{#if sheetMode}<ChevronDown size={16} class="ctx-chip-chev" aria-hidden="true" />{/if}</button>
	{/if}
	{#if showDropdown}
		<div
			id={pickerId}
			class="picker-dropdown sentence-menu"
			class:sheet-page={sheetMode}
			role={searchable ? 'dialog' : undefined}
			aria-label={searchable ? (selectedId ? 'Switch project' : 'Add a project') : undefined}
			{...{ [PROJECT_PICKER_ATTRIBUTE]: searchable ? '' : undefined }}
			bind:this={listRef}
			use:mobilePopup
			use:hostMenu={sheetMode ? menuHost : null}
			use:floatingMenu={{ anchor: anchorOverride ?? sentenceTrigger, align: 'start', enabled: !isMobileUi() && !sheetMode, keepPlacement: searchable }}
		>
			<GroupedSelector
				bind:this={groupedSelector}
				items={pickerItems}
				entityType="projects"
				selectedId={selectedId}
				onSelect={handleSelect}
				label={selectedId ? 'Switch project' : 'Add a project'}
				addLabel="Add Project"
				onAdd={openAddModal}
				emptyMessage="No projects yet"
				itemIcon={FolderClosed}
				searchable={searchable}
				searchLabel="Search projects"
				rank={searchable ? rankProjects : undefined}
				numbered={searchable}
				trailingItem={noProjectRow}
				trailingIcon={FolderX}
				autofocus={searchable && autofocusSearch}
			/>
			<p class="sentence-hint">
				<span>Optional · adds checkout, branch and git</span>
				{#if searchable && !isMobileUi()}<kbd class="shortcut-hint" aria-hidden="true">{shortcutHint}</kbd>{/if}
			</p>
		</div>
	{/if}
</span>
{:else}
<div class="workspace-selector">
	<!-- Logo / Icon -->
	<div class="logo-mark">
		<svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
			<path d="M20 4C11.16 4 4 11.16 4 20s7.16 16 16 16 16-7.16 16-16S28.84 4 20 4zm0 28c-6.63 0-12-5.37-12-12S13.37 8 20 8s12 5.37 12 12-5.37 12-12 12z" fill="currentColor" opacity="0.2"/>
			<path d="M15 17c0-1.1.9-2 2-2h6c1.1 0 2 .9 2 2v6c0 1.1-.9 2-2 2h-6c-1.1 0-2-.9-2-2v-6z" fill="currentColor" opacity="0.5"/>
			<circle cx="20" cy="20" r="3" fill="currentColor"/>
		</svg>
	</div>

	<!-- Heading -->
	<h1 class="hero-heading">Let's build</h1>

	<!-- Workspace Picker -->
	<div class="picker-container" bind:this={dropdownRef}>
		<button
			type="button"
			class="picker-trigger"
			aria-label={triggerLabel}
			aria-expanded={showDropdown}
			aria-controls={pickerId}
			onclick={toggleDropdown}
		>
			<span class="picker-name">{selectedProjectName}</span>
			<ChevronDown
				size={18}
				strokeWidth={1.5}
				class="picker-chevron {showDropdown ? 'flipped' : ''}"
			/>
		</button>

		{#if showDropdown}
			<div id={pickerId} class="picker-dropdown" use:mobilePopup>
				<GroupedSelector
					bind:this={groupedSelector}
					items={pickerItems}
					entityType="projects"
					selectedId={selectedId}
					onSelect={handleSelect}
					label="Select your workspace"
					addLabel="Add Project"
					onAdd={openAddModal}
					emptyMessage="No workspaces available"
					itemIcon={FolderClosed}
				/>
			</div>
		{/if}
	</div>

	{#if $profilesLoaded && $profiles.length === 0}
		<!-- Only once the roster has answered: before that an empty list is a
		     load in progress, not a missing agent. Nothing is seeded, so a fresh install lands here with an empty agent
		     picker and a send that can only fail. Say what is missing where the
		     user is already looking, rather than at submit time. -->
		<button type="button" class="no-agents" onclick={() => layout.openFeaturePage('profile')}>
			No agents yet — create one to start chatting
		</button>
	{/if}
</div>
{/if}

<!-- Add Project Modal -->
<AddProjectModal
	open={showAddModal}
	onClose={() => (showAddModal = false)}
	onCreated={(id) => onSelectProject(id)}
/>

<style>
	.workspace-selector {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
		padding: var(--workspace-hero-padding, 2rem);
		user-select: none;
	}

	/* Logo mark */
	.logo-mark {
		color: var(--foreground);
		margin-bottom: 8px;
		opacity: 0.8;
	}

	/* Hero heading */
	.hero-heading {
		font-family: var(--font-display, Georgia, serif);
		font-size: 2rem;
		font-weight: 700;
		color: var(--foreground);
		letter-spacing: -0.02em;
		line-height: 1.1;
	}

	/* Workspace picker trigger */
	.picker-container {
		position: relative;
	}

	.no-agents {
		margin-top: 4px;
		background: none;
		border: none;
		padding: 0;
		color: var(--primary);
		cursor: pointer;
		font-size: 0.8125rem;
	}

	.no-agents:hover {
		text-decoration: underline;
	}

	.picker-trigger {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 4px 8px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: opacity 150ms ease;
	}

	.picker-trigger:hover {
		opacity: 0.7;
	}

	.picker-name {
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.75rem;
		font-weight: 400;
		color: var(--muted-foreground);
		letter-spacing: -0.01em;
	}

	.picker-trigger :global(.picker-chevron) {
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.picker-trigger :global(.picker-chevron.flipped) {
		transform: rotate(180deg);
	}

	/* Dropdown */
	.picker-dropdown {
		position: absolute;
		top: calc(100% + 8px);
		left: 50%;
		transform: translateX(-50%);
		min-width: 280px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		padding: 8px;
		z-index: 100;
		animation: dropdownAppear 150ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes dropdownAppear {
		from {
			opacity: 0;
			transform: translateX(-50%) translateY(-4px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateX(-50%) translateY(0) scale(1);
		}
	}

	/* Reduced motion */
	.picker-dropdown:global(.mobile-popup) {
		position: fixed;
		inset: auto;
		top: var(--picker-top);
		left: var(--picker-left);
		width: min(320px, calc(100vw - 24px));
		min-width: 0;
		margin: 0;
		transform: none;
		animation: none;
	}

	/* ===== Hero sentence word (variant="sentence") ===== */
	.sentence-variant { display: inline-flex; position: static; }
	/* floatingMenu owns its position on desktop; mobilePopup on the phone. */
	/* On the sheet's Project page the list fills the page instead of floating.
	   Three classes so it outranks the floating `.sentence-menu` width below;
	   the page scrolls, not a capped list inside it. */
	.picker-dropdown.sentence-menu.sheet-page {
		--selector-scroll-height: none;
		position: static;
		width: 100%;
		min-width: 0;
		max-width: none;
		margin: 0;
		transform: none;
		border: 0;
		background: transparent;
		box-shadow: none;
		animation: none;
	}
	.picker-dropdown.sentence-menu {
		transform: none;
		width: 300px;
		max-width: calc(100vw - 24px);
		background: var(--popover);
		animation-name: sentence-dropdown-in;
	}
	.sentence-hint {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin: 4px 0 0;
		padding: 6px 8px 2px;
		border-top: 1px solid var(--border-subtle);
		font-size: 11px;
		color: var(--muted-foreground);
	}
	.shortcut-hint {
		flex-shrink: 0;
		color: var(--subtle-foreground);
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 10.5px;
	}
	@keyframes sentence-dropdown-in {
		from { opacity: 0; transform: translateY(-4px) scale(0.98); }
		to { opacity: 1; transform: none; }
	}

	@media (prefers-reduced-motion: reduce) {
		.picker-dropdown {
			animation: none;
		}
	}
</style>
