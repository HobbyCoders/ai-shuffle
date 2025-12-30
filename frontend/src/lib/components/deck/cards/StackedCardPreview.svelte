<script lang="ts">
	/**
	 * StackedCardPreview - Compact card preview for stack layout
	 *
	 * Shows a condensed view of a card when it's in the stacked deck (not the main card).
	 * - Chat cards: Icon + title + last message preview
	 * - Other cards: Icon + title only
	 *
	 * Features:
	 * - Click to make this card the main card (triggers shuffle animation)
	 * - Drag to reorder within the stack
	 * - Hover effect with subtle slide-right animation
	 */

	import {
		MessageSquare,
		Bot,
		Terminal,
		Settings,
		User,
		Users,
		FolderKanban,
		Puzzle,
		Image,
		Box,
		AudioLines,
		FolderOpen
	} from 'lucide-svelte';
	import { allTabs } from '$lib/stores/tabs';
	import type { DeckCard, CardType } from './types';

	interface Props {
		card: DeckCard;
		tabId?: string | null;
		onClick: () => void;
		onDragStart?: (e: PointerEvent) => void;
		onDragMove?: (e: PointerEvent) => void;
		onDragEnd?: () => void;
		isDragging?: boolean;
	}

	let {
		card,
		tabId = null,
		onClick,
		onDragStart,
		onDragMove,
		onDragEnd,
		isDragging = false
	}: Props = $props();

	// Icon mapping for card types
	const cardIcons: Record<CardType, typeof MessageSquare> = {
		chat: MessageSquare,
		agent: Bot,
		terminal: Terminal,
		settings: Settings,
		profile: User,
		subagent: Users,
		project: FolderKanban,
		plugins: Puzzle,
		'user-settings': Settings,
		'image-studio': Image,
		'model-studio': Box,
		'audio-studio': AudioLines,
		'file-browser': FolderOpen
	};

	const CardIcon = $derived(cardIcons[card.type] || MessageSquare);

	// Get tab data for chat cards
	const tab = $derived(card.type === 'chat' && tabId ? $allTabs.find((t) => t.id === tabId) : null);

	// Get the last message for chat preview
	const lastMessage = $derived(tab?.messages[tab.messages.length - 1] ?? null);

	// Format message preview - truncate and clean up
	const messagePreview = $derived.by(() => {
		if (!lastMessage) return 'New conversation';

		let content = lastMessage.content || '';

		// If it's a tool result, show a simpler message
		if (lastMessage.type === 'tool_result' || lastMessage.type === 'tool_use') {
			return `Using ${lastMessage.toolName || 'tool'}...`;
		}

		// Clean up content - remove markdown, code blocks, etc.
		content = content
			.replace(/```[\s\S]*?```/g, '[code]') // Code blocks
			.replace(/`[^`]+`/g, '[code]') // Inline code
			.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // Links
			.replace(/[#*_~]/g, '') // Markdown formatting
			.replace(/\n+/g, ' ') // Newlines to spaces
			.trim();

		// Truncate
		if (content.length > 80) {
			return content.substring(0, 77) + '...';
		}
		return content || 'New conversation';
	});

	// Message count for chat cards
	const messageCount = $derived(tab?.messages.length ?? 0);

	// Is this an empty chat?
	const isEmptyChat = $derived(card.type === 'chat' && messageCount === 0);

	// Determine subtitle based on card type
	const subtitle = $derived.by(() => {
		switch (card.type) {
			case 'chat':
				return messagePreview;
			case 'terminal':
				return 'Terminal session';
			case 'settings':
				return 'Application settings';
			case 'profile':
				return 'Profile management';
			case 'project':
				return 'Project configuration';
			case 'plugins':
				return 'Plugin manager';
			case 'user-settings':
				return 'User preferences';
			case 'image-studio':
				return 'Image generation';
			case 'model-studio':
				return '3D model creation';
			case 'audio-studio':
				return 'Audio & speech';
			case 'file-browser':
				return 'File explorer';
			case 'subagent':
				return 'Agent management';
			default:
				return '';
		}
	});

	// Drag state
	let dragStartPos = $state({ x: 0, y: 0 });

	function handlePointerDown(e: PointerEvent) {
		// Don't start drag on button clicks
		if ((e.target as HTMLElement).closest('button')) return;

		dragStartPos = { x: e.clientX, y: e.clientY };
		onDragStart?.(e);
	}

	function handlePointerMove(e: PointerEvent) {
		onDragMove?.(e);
	}

	function handlePointerUp(e: PointerEvent) {
		// Check if this was a click (minimal movement) or a drag
		const dx = Math.abs(e.clientX - dragStartPos.x);
		const dy = Math.abs(e.clientY - dragStartPos.y);

		if (dx < 5 && dy < 5) {
			// This was a click, not a drag
			onClick();
		}

		onDragEnd?.();
	}

	function handleClick(e: MouseEvent) {
		// Prevent double-firing with pointer events
		e.preventDefault();
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
	class="stacked-card-preview"
	class:dragging={isDragging}
	class:empty={isEmptyChat}
	class:focused={card.focused}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	onclick={handleClick}
	role="button"
	tabindex="0"
	onkeydown={(e) => {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			onClick();
		}
	}}
>
	<!-- Card icon -->
	<div class="card-icon" class:chat={card.type === 'chat'}>
		<CardIcon size={18} />
	</div>

	<!-- Content area -->
	<div class="content">
		<div class="title">{card.title}</div>
		<div class="subtitle" class:empty={isEmptyChat}>
			{subtitle}
		</div>
	</div>

	<!-- Message count badge for chat cards -->
	{#if card.type === 'chat' && messageCount > 0}
		<div class="badge">
			{messageCount}
		</div>
	{/if}
</div>

<style>
	.stacked-card-preview {
		display: flex;
		align-items: center;
		gap: 12px;
		height: 100%;
		padding: 12px 14px;
		background: var(--glass-bg);
		backdrop-filter: blur(12px);
		-webkit-backdrop-filter: blur(12px);
		border-radius: 10px;
		border: 1px solid var(--glass-border);
		cursor: pointer;
		user-select: none;
		transition:
			transform 0.15s cubic-bezier(0.4, 0, 0.2, 1),
			box-shadow 0.15s cubic-bezier(0.4, 0, 0.2, 1),
			border-color 0.15s cubic-bezier(0.4, 0, 0.2, 1),
			background 0.15s cubic-bezier(0.4, 0, 0.2, 1);
		overflow: hidden;
	}

	.stacked-card-preview:hover:not(.dragging) {
		transform: translateX(6px);
		box-shadow:
			var(--shadow-m),
			0 4px 16px color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: color-mix(in oklch, var(--primary) 30%, var(--glass-border));
		background: color-mix(in oklch, var(--primary) 5%, var(--glass-bg));
	}

	.stacked-card-preview:active:not(.dragging) {
		transform: translateX(4px) scale(0.98);
	}

	.stacked-card-preview.dragging {
		opacity: 0.9;
		transform: scale(1.02);
		box-shadow:
			var(--shadow-l),
			0 12px 32px rgba(0, 0, 0, 0.25);
		border-color: var(--primary);
		cursor: grabbing;
		transition: none;
	}

	.stacked-card-preview.focused {
		border-color: color-mix(in oklch, var(--primary) 50%, var(--glass-border));
		box-shadow:
			var(--shadow-m),
			0 0 0 1px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	/* Card icon */
	.card-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition:
			background 0.15s ease,
			color 0.15s ease;
	}

	.stacked-card-preview:hover .card-icon {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.card-icon.chat {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	/* Content */
	.content {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.title {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		line-height: 1.3;
	}

	.subtitle {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		line-height: 1.4;
	}

	.subtitle.empty {
		font-style: italic;
		color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
	}

	/* Badge */
	.badge {
		display: flex;
		align-items: center;
		justify-content: center;
		min-width: 20px;
		height: 20px;
		padding: 0 6px;
		border-radius: 10px;
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		color: var(--primary);
		font-size: 0.6875rem;
		font-weight: 600;
		flex-shrink: 0;
	}

	/* Focus state for accessibility */
	.stacked-card-preview:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
</style>
