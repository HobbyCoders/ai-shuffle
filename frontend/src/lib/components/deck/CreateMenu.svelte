<script lang="ts">
	/**
	 * CreateMenu - Card Fan-Out menu for creating new cards
	 * Casino-themed dropdown with staggered card dealing animation
	 */
	import { MessageSquare, Terminal, User, FolderKanban, Settings, UserCog, Bot, Image, Box, AudioLines, Files } from 'lucide-svelte';

	interface Props {
		open: boolean;
		onClose: () => void;
		onCreateChat: () => void;
		onCreateTerminal: () => void;
		onOpenProfiles: () => void;
		onOpenProjects: () => void;
		onOpenSubagents: () => void;
		onOpenSettings: () => void;
		onOpenUserSettings?: () => void;
		onOpenImageStudio?: () => void;
		onOpenModelStudio?: () => void;
		onOpenAudioStudio?: () => void;
		onOpenFileBrowser?: () => void;
		isAdmin?: boolean;
		anchorPosition?: 'left' | 'right';
		isMobile?: boolean;
	}

	let {
		open,
		onClose,
		onCreateChat,
		onCreateTerminal,
		onOpenProfiles,
		onOpenProjects,
		onOpenSubagents,
		onOpenSettings,
		onOpenUserSettings,
		onOpenImageStudio,
		onOpenModelStudio,
		onOpenAudioStudio,
		onOpenFileBrowser,
		isAdmin = false,
		anchorPosition = 'left',
		isMobile = false
	}: Props = $props();

	// Suit assignments for visual variety
	const suits = ['♠', '♦', '♥', '♣'] as const;

	// Build menu items based on user role
	const menuItems = $derived.by(() => {
		const items: Array<{
			id: string;
			label: string;
			icon: typeof MessageSquare | null;
			shortcut: string;
			action: () => void;
			suit: string;
		}> = [
			{ id: 'chat', label: 'New Chat', icon: MessageSquare, shortcut: '⌘N', action: onCreateChat, suit: '♠' },
			{ id: 'terminal', label: 'Terminal', icon: Terminal, shortcut: '⌘T', action: onCreateTerminal, suit: '♥' },
			{ id: 'divider1', label: '', icon: null, shortcut: '', action: () => {}, suit: '' },
			// Studio cards
			{ id: 'image-studio', label: 'Image Studio', icon: Image, shortcut: '', action: onOpenImageStudio || (() => {}), suit: '♥' },
			{ id: 'model-studio', label: '3D Models', icon: Box, shortcut: '', action: onOpenModelStudio || (() => {}), suit: '♣' },
			{ id: 'audio-studio', label: 'Audio Studio', icon: AudioLines, shortcut: '', action: onOpenAudioStudio || (() => {}), suit: '♠' },
			{ id: 'file-browser', label: 'Files', icon: Files, shortcut: '', action: onOpenFileBrowser || (() => {}), suit: '♦' },
			{ id: 'divider2', label: '', icon: null, shortcut: '', action: () => {}, suit: '' }
		];

		if (isAdmin) {
			items.push(
				{ id: 'profiles', label: 'Profiles', icon: User, shortcut: '⌘⇧P', action: onOpenProfiles, suit: '♥' },
				{ id: 'projects', label: 'Projects', icon: FolderKanban, shortcut: '⌘⇧J', action: onOpenProjects, suit: '♣' },
				{ id: 'subagents', label: 'Subagents', icon: Bot, shortcut: '⌘⇧A', action: onOpenSubagents, suit: '♠' },
				{ id: 'settings', label: 'Settings', icon: Settings, shortcut: '⌘,', action: onOpenSettings, suit: '♦' }
			);
		} else {
			items.push(
				{ id: 'user-settings', label: 'My Settings', icon: UserCog, shortcut: '', action: onOpenUserSettings || (() => {}), suit: '♥' }
			);
		}

		return items;
	});

	function handleItemClick(item: typeof menuItems[0]) {
		item.action();
		onClose();
	}

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			onClose();
		}
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose();
		}
	}
</script>

<svelte:window onkeydown={handleKeyDown} />

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div class="menu-backdrop" onclick={handleBackdropClick}>
		<div
			class="create-menu"
			class:mobile={isMobile}
			class:anchor-left={anchorPosition === 'left'}
			class:anchor-right={anchorPosition === 'right'}
			role="menu"
			aria-label="Deal new card"
		>
			<!-- Menu Cards - Fan out animation -->
			{#each menuItems as item, index}
				{#if item.id.startsWith('divider')}
					<div class="menu-divider" style="--card-index: {index}"></div>
				{:else}
					<button
						type="button"
						class="menu-card"
						style="--card-index: {index}"
						data-suit={item.suit}
						onclick={() => handleItemClick(item)}
						role="menuitem"
					>
						<div class="menu-card-icon">
							{#if item.icon}
								<item.icon size={18} strokeWidth={1.5} />
							{/if}
						</div>
						<div class="menu-card-content">
							<span class="menu-card-label">{item.label}</span>
							{#if item.shortcut && !isMobile}
								<span class="menu-card-shortcut">{item.shortcut}</span>
							{/if}
						</div>
					</button>
				{/if}
			{/each}
		</div>
	</div>
{/if}

<style>
	/* ===================================
	   MENU BACKDROP
	   =================================== */

	.menu-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100000;
		background: rgba(0, 0, 0, 0.3);
		backdrop-filter: blur(2px);
		-webkit-backdrop-filter: blur(2px);
		opacity: 0;
		animation: backdropFadeIn 0.2s ease forwards;
	}

	@keyframes backdropFadeIn {
		to { opacity: 1; }
	}

	/* ===================================
	   CREATE MENU CONTAINER
	   =================================== */

	.create-menu {
		position: fixed;
		bottom: 96px; /* Above the floating dealer button */
		display: flex;
		flex-direction: column;
		gap: 0;
		z-index: 100001;
	}

	.create-menu.anchor-left {
		left: 24px;
	}

	.create-menu.anchor-right {
		right: 24px;
	}

	.create-menu.mobile {
		position: fixed;
		bottom: 100px;
		left: 50%;
		transform: translateX(-50%);
	}

	/* ===================================
	   MENU CARD - Playing card style
	   =================================== */

	.menu-card {
		--card-index: 0;

		position: relative;
		display: flex;
		align-items: center;
		gap: 14px;
		width: 220px;
		padding: 14px 18px;
		background: linear-gradient(135deg,
			var(--menu-card-bg) 0%,
			oklch(0.12 0.01 260) 100%
		);
		border: 1px solid var(--menu-card-border);
		border-radius: 12px;
		cursor: pointer;
		color: var(--foreground);
		font-size: 0.875rem;
		font-weight: 500;
		text-align: left;

		/* Card shadow */
		box-shadow:
			0 4px 16px rgba(0, 0, 0, 0.3),
			0 2px 4px rgba(0, 0, 0, 0.2),
			inset 0 1px 0 rgba(255, 255, 255, 0.05);

		/* Initial state - stacked behind dealer button */
		opacity: 0;
		transform: translateX(-60px) translateY(calc(var(--card-index) * 4px))
			rotate(calc(-8deg + var(--card-index) * 2deg)) scale(0.6);
		transform-origin: left center;

		/* Staggered deal animation */
		animation: dealCard 0.4s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
		animation-delay: calc(var(--card-index) * 0.05s);

		transition:
			background 0.15s ease,
			border-color 0.15s ease,
			box-shadow 0.15s ease,
			transform 0.15s ease;
	}

	/* Card suit watermark */
	.menu-card::before {
		content: attr(data-suit);
		position: absolute;
		top: 6px;
		right: 10px;
		font-size: 12px;
		color: var(--gold);
		opacity: 0.25;
		transition: opacity 0.2s ease;
	}

	.menu-card:hover {
		background: linear-gradient(135deg,
			oklch(0.18 0.015 260) 0%,
			oklch(0.14 0.01 260) 100%
		);
		border-color: var(--gold-dim);
		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.4),
			0 4px 8px rgba(0, 0, 0, 0.3),
			0 0 20px var(--gold-glow),
			inset 0 1px 0 rgba(255, 255, 255, 0.08);
		transform: translateX(8px) scale(1.02);
	}

	.menu-card:hover::before {
		opacity: 0.5;
	}

	.menu-card:active {
		transform: translateX(4px) scale(0.98);
		transition-duration: 0.08s;
	}

	/* Deal animation - cards fly out from dealer button */
	@keyframes dealCard {
		0% {
			opacity: 0;
			transform: translateX(-60px) translateY(calc(var(--card-index) * 4px))
				rotate(calc(-8deg + var(--card-index) * 2deg)) scale(0.6);
		}
		60% {
			opacity: 1;
			transform: translateX(8px) translateY(0)
				rotate(calc(2deg - var(--card-index) * 0.5deg)) scale(1.02);
		}
		100% {
			opacity: 1;
			transform: translateX(0) translateY(0) rotate(0deg) scale(1);
		}
	}

	/* ===================================
	   MENU CARD ICON
	   =================================== */

	.menu-card-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: 8px;
		background: color-mix(in srgb, var(--gold) 10%, transparent);
		color: var(--gold);
		flex-shrink: 0;
		transition: all 0.2s ease;
	}

	.menu-card:hover .menu-card-icon {
		background: color-mix(in srgb, var(--gold) 20%, transparent);
		box-shadow: 0 0 12px var(--gold-glow);
	}

	.menu-card-icon :global(svg) {
		width: 18px;
		height: 18px;
	}

	/* ===================================
	   MENU CARD CONTENT
	   =================================== */

	.menu-card-content {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.menu-card-label {
		font-weight: 500;
	}

	.menu-card-shortcut {
		font-size: 0.6875rem;
		font-family: ui-monospace, 'Monaco', 'Menlo', monospace;
		color: var(--muted-foreground);
		opacity: 0.6;
	}

	/* ===================================
	   MENU DIVIDER
	   =================================== */

	.menu-divider {
		--card-index: 0;

		width: 200px;
		height: 1px;
		margin: 6px 10px;
		background: linear-gradient(90deg,
			transparent 0%,
			var(--gold-dim) 30%,
			var(--gold-dim) 70%,
			transparent 100%
		);
		opacity: 0;
		animation: fadeInDivider 0.3s ease forwards;
		animation-delay: calc(var(--card-index) * 0.05s + 0.05s);
	}

	@keyframes fadeInDivider {
		to { opacity: 0.4; }
	}

	/* ===================================
	   MOBILE ADJUSTMENTS
	   =================================== */

	.create-menu.mobile .menu-card {
		width: calc(100vw - 64px);
		max-width: 300px;
		padding: 16px 18px;
		font-size: 1rem;
	}

	.create-menu.mobile .menu-card-icon {
		width: 36px;
		height: 36px;
	}

	.create-menu.mobile .menu-card-icon :global(svg) {
		width: 20px;
		height: 20px;
	}

	/* Mobile deal animation - from bottom */
	.create-menu.mobile .menu-card {
		transform: translateY(40px) scale(0.9);
		animation: dealCardMobile 0.35s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
		animation-delay: calc(var(--card-index) * 0.04s);
	}

	@keyframes dealCardMobile {
		0% {
			opacity: 0;
			transform: translateY(40px) scale(0.9);
		}
		100% {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.create-menu.mobile .menu-card:hover {
		transform: scale(1.02);
	}

	/* ===================================
	   REDUCED MOTION
	   =================================== */

	@media (prefers-reduced-motion: reduce) {
		.menu-backdrop {
			animation: none;
			opacity: 1;
		}

		.menu-card {
			animation: none;
			opacity: 1;
			transform: none;
		}

		.menu-divider {
			animation: none;
			opacity: 0.4;
		}
	}
</style>
