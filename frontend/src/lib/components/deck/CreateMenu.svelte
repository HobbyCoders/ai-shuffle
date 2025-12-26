<script lang="ts">
	/**
	 * CreateMenu - Dropdown menu for creating new cards
	 * Shows card type options with icons and keyboard shortcuts
	 */
	import { MessageSquare, Terminal, User, FolderKanban, Settings, UserCog, Bot, X } from 'lucide-svelte';

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
		isAdmin = false,
		anchorPosition = 'left',
		isMobile = false
	}: Props = $props();

	// Build menu items based on user role
	const menuItems = $derived.by(() => {
		const items = [
			{ id: 'chat', label: 'New Chat', icon: MessageSquare, shortcut: '⌘N', action: onCreateChat },
			{ id: 'terminal', label: 'Terminal', icon: Terminal, shortcut: '⌘T', action: onCreateTerminal },
			{ id: 'divider', label: '', icon: null, shortcut: '', action: () => {} }
		];

		if (isAdmin) {
			// Admin gets full access to profiles, projects, subagents, and admin settings
			items.push(
				{ id: 'profiles', label: 'Profiles', icon: User, shortcut: '⌘⇧P', action: onOpenProfiles },
				{ id: 'projects', label: 'Projects', icon: FolderKanban, shortcut: '⌘⇧J', action: onOpenProjects },
				{ id: 'subagents', label: 'Subagents', icon: Bot, shortcut: '⌘⇧A', action: onOpenSubagents },
				{ id: 'settings', label: 'Settings', icon: Settings, shortcut: '⌘,', action: onOpenSettings }
			);
		} else {
			// Non-admin users get their own settings
			items.push(
				{ id: 'user-settings', label: 'My Settings', icon: UserCog, shortcut: '', action: onOpenUserSettings || (() => {}) }
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
			aria-label="Create new item"
		>
			<!-- Header -->
			<div class="menu-header">
				<span class="header-title">Create New</span>
				<button
					type="button"
					class="close-btn"
					onclick={onClose}
					aria-label="Close menu"
				>
					<X size={16} />
				</button>
			</div>

			<!-- Menu Items -->
			<div class="menu-items">
				{#each menuItems as item}
					{#if item.id === 'divider'}
						<div class="menu-divider"></div>
					{:else}
						<button
							type="button"
							class="menu-item"
							onclick={() => handleItemClick(item)}
							role="menuitem"
						>
							{#if item.icon}
								<item.icon size={18} strokeWidth={1.5} />
							{/if}
							<span class="item-label">{item.label}</span>
							{#if item.shortcut && !isMobile}
								<span class="item-shortcut">{item.shortcut}</span>
							{/if}
						</button>
					{/if}
				{/each}
			</div>
		</div>
	</div>
{/if}

<style>
	.menu-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100000;
		background: transparent;
	}

	.create-menu {
		position: fixed;
		top: 12px;
		min-width: 200px;
		max-width: 280px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 12px;
		box-shadow: 0 8px 32px -4px rgba(0, 0, 0, 0.2), 0 4px 8px rgba(0, 0, 0, 0.1);
		overflow: hidden;
		animation: menuSlideIn 0.15s ease-out;
	}

	.create-menu.anchor-left {
		left: 76px; /* 64px rail width + 12px padding */
	}

	.create-menu.anchor-right {
		right: 12px;
	}

	.create-menu.mobile {
		position: fixed;
		top: auto;
		bottom: 80px;
		left: 50%;
		transform: translateX(-50%);
		width: calc(100% - 32px);
		max-width: 320px;
		animation: menuSlideUp 0.2s ease-out;
	}

	@keyframes menuSlideIn {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	@keyframes menuSlideUp {
		from {
			opacity: 0;
			transform: translate(-50%, 16px);
		}
		to {
			opacity: 1;
			transform: translate(-50%, 0);
		}
	}

	:global(.dark) .create-menu {
		background: oklch(0.18 0.01 260);
		border-color: oklch(0.28 0.01 260);
	}

	.menu-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border);
	}

	.header-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border-radius: 6px;
		color: var(--muted-foreground);
		transition: background-color 0.15s, color 0.15s;
	}

	.close-btn:hover {
		background: var(--accent);
		color: var(--foreground);
	}

	.menu-items {
		padding: 8px;
	}

	.menu-item {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 10px 12px;
		font-size: 0.875rem;
		color: var(--foreground);
		background: transparent;
		border-radius: 8px;
		transition: background-color 0.1s;
		text-align: left;
	}

	.menu-item:hover {
		background: var(--accent);
	}

	.menu-item :global(svg) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.menu-item:hover :global(svg) {
		color: var(--foreground);
	}

	.item-label {
		flex: 1;
	}

	.item-shortcut {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
		opacity: 0.7;
	}

	.menu-divider {
		height: 1px;
		margin: 8px 4px;
		background: var(--border);
	}

	/* Mobile adjustments */
	.create-menu.mobile .menu-item {
		padding: 14px 12px;
		font-size: 1rem;
	}

	.create-menu.mobile .menu-header {
		padding: 14px 16px;
	}
</style>
