<script lang="ts">
	/**
	 * CardDeckNavigator - AI Shuffle themed card navigation overlay
	 *
	 * Full-screen card carousel for browsing and managing:
	 * - Quick actions (new chat, new agent, terminal, etc.)
	 * - Recent threads/conversations
	 * - Custom decks (user-created groupings)
	 * - Studio tools
	 *
	 * Features:
	 * - Horizontal scroll with snap
	 * - Sub-deck drill-down with breadcrumb navigation
	 * - Mobile swipe gestures
	 * - AI streaming state indicators
	 * - Edit mode for reordering cards
	 * - Custom deck management
	 */

	import { onMount } from 'svelte';
	import {
		MessageSquare, Terminal, Clock, Image, Box, AudioLines,
		Settings, FolderOpen, X, Files, User, Pencil, Check,
		GripVertical, Plus, Folder, Trash2, MoreVertical, ChevronRight, Bot
	} from 'lucide-svelte';
	import { tabs, type Session } from '$lib/stores/tabs';
	import { deck, type DeckCard as DeckCardType } from '$lib/stores/deck';
	import { navigator, type CustomDeck, DECK_COLORS } from '$lib/stores/navigator';
	import { fly, fade, scale } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { flip } from 'svelte/animate';

	interface Props {
		open: boolean;
		onClose: () => void;
		onCreateChat: () => void;
		onCreateTerminal: () => void;
		onOpenThread: (sessionId: string) => void;
		onOpenImageStudio?: () => void;
		onOpenModelStudio?: () => void;
		onOpenAudioStudio?: () => void;
		onOpenFileBrowser?: () => void;
		onOpenProjects?: () => void;
		onOpenProfiles?: () => void;
		onOpenSubagents?: () => void;
		onOpenSettings?: () => void;
		isAdmin?: boolean;
		isMobile?: boolean;
	}

	let {
		open,
		onClose,
		onCreateChat,
		onCreateTerminal,
		onOpenThread,
		onOpenImageStudio,
		onOpenModelStudio,
		onOpenAudioStudio,
		onOpenFileBrowser,
		onOpenProjects,
		onOpenProfiles,
		onOpenSubagents,
		onOpenSettings,
		isAdmin = false,
		isMobile = false
	}: Props = $props();

	// Navigation stack for breadcrumb
	interface NavItem {
		id: string;
		name: string;
		type: 'root' | 'category' | 'deck';
	}

	let navStack = $state<NavItem[]>([{ id: 'root', name: 'AI Shuffle', type: 'root' }]);
	let currentView = $state<'main' | 'recent' | 'deck'>('main');
	let currentDeckId = $state<string | null>(null);

	// Carousel state
	let carouselRef = $state<HTMLDivElement | null>(null);
	let isDragging = $state(false);
	let hasDragged = $state(false);
	let startX = $state(0);
	let scrollLeft = $state(0);
	let scrollProgress = $state(0);
	const DRAG_THRESHOLD = 5;

	// Edit mode state
	let isEditMode = $state(false);
	let draggedCardId = $state<string | null>(null);
	let draggedCardIndex = $state<number>(-1);
	let dragOverIndex = $state<number>(-1);
	let longPressTimer = $state<ReturnType<typeof setTimeout> | null>(null);
	const LONG_PRESS_DURATION = 500;

	// Pointer-based drag state for smooth animation
	let isPointerDragging = $state(false);
	let pointerDragPosition = $state({ x: 0, y: 0 });
	let pointerDragOffset = $state({ x: 0, y: 0 });
	let draggedCardElement = $state<HTMLElement | null>(null);
	let didDragInEditMode = $state(false); // Track if drag occurred to prevent click
	let dragStartPosition = $state({ x: 0, y: 0 }); // Track start position for drag threshold
	let railYPosition = $state(0); // Y position to lock horizontal dragging
	const DRAG_START_THRESHOLD = 8; // Pixels to move before drag starts

	// Live reorder state - tracks the current visual order during drag
	let liveCardOrder = $state<string[]>([]);
	// Original card positions captured at drag start (before any reordering)
	let originalCardPositions = $state<{ id: string; left: number; width: number }[]>([]);

	// Context menu state
	let contextMenuOpen = $state(false);
	let contextMenuCardId = $state<string | null>(null);
	let contextMenuPosition = $state({ x: 0, y: 0 });
	let contextMenuIsDeck = $state(false); // Track if context menu is for a deck card

	// New deck dialog state
	let showNewDeckDialog = $state(false);
	let newDeckName = $state('');

	// Rename deck dialog state
	let showRenameDeckDialog = $state(false);
	let renameDeckId = $state<string | null>(null);
	let renameDeckName = $state('');

	// Card types for main deck
	type CardAction = 'new-chat' | 'terminal' | 'recent' | 'image-studio' | 'model-studio' | 'audio-studio' | 'file-browser' | 'projects' | 'profiles' | 'subagents' | 'settings';

	interface NavigatorCard {
		id: string;
		type: 'action' | 'category' | 'thread' | 'deck' | 'add-deck';
		action?: CardAction;
		category?: string;
		deckId?: string;
		title: string;
		subtitle: string;
		icon: typeof MessageSquare;
		iconColor?: string;
		hasChildren?: boolean;
		sessionId?: string;
		isStreaming?: boolean;
		messageCount?: number;
		lastActive?: string;
	}

	// Get navigator state
	const navigatorState = $derived($navigator);
	const userDecks = $derived(navigatorState.decks);

	// Get sessions from tabs store
	const sessions = $derived($tabs.sessions);

	// Get active streaming cards
	const activeCards = $derived($deck.cards.filter(c => c.type === 'chat'));

	// Check if a session is currently streaming
	function isSessionStreaming(sessionId: string): boolean {
		const tab = $tabs.tabs.find(t => t.sessionId === sessionId);
		return tab?.isStreaming ?? false;
	}

	// Format relative time
	function formatRelativeTime(dateStr: string): string {
		const date = new Date(dateStr);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffMins = Math.floor(diffMs / 60000);
		const diffHours = Math.floor(diffMs / 3600000);
		const diffDays = Math.floor(diffMs / 86400000);

		if (diffMins < 1) return 'Just now';
		if (diffMins < 60) return `${diffMins}m ago`;
		if (diffHours < 24) return `${diffHours}h ago`;
		if (diffDays < 7) return `${diffDays}d ago`;
		return date.toLocaleDateString();
	}

	// Get all card IDs that are in any deck
	const cardsInDecks = $derived.by((): Set<string> => {
		const ids = new Set<string>();
		for (const deck of userDecks) {
			for (const cardId of deck.cardIds) {
				ids.add(cardId);
			}
		}
		return ids;
	});

	// Build base action cards (before filtering by deck membership)
	const baseActionCards = $derived.by((): NavigatorCard[] => {
		const cards: NavigatorCard[] = [
			{
				id: 'new-chat',
				type: 'action',
				action: 'new-chat',
				title: 'New Chat',
				subtitle: 'Start a conversation with AI',
				icon: MessageSquare
			},
			{
				id: 'terminal',
				type: 'action',
				action: 'terminal',
				title: 'Terminal',
				subtitle: 'Command line interface',
				icon: Terminal
			},
			{
				id: 'recent',
				type: 'category',
				category: 'recent',
				title: 'Recent',
				subtitle: `${sessions.length} conversations`,
				icon: Clock,
				hasChildren: true
			},
			{
				id: 'image-studio',
				type: 'action',
				action: 'image-studio',
				title: 'Image Studio',
				subtitle: 'Generate & edit images',
				icon: Image
			},
			{
				id: 'model-studio',
				type: 'action',
				action: 'model-studio',
				title: '3D Models',
				subtitle: 'Generate 3D assets',
				icon: Box
			},
			{
				id: 'audio-studio',
				type: 'action',
				action: 'audio-studio',
				title: 'Audio Studio',
				subtitle: 'Text to speech & more',
				icon: AudioLines
			},
			{
				id: 'file-browser',
				type: 'action',
				action: 'file-browser',
				title: 'Files',
				subtitle: 'Browse & manage files',
				icon: Files
			},
			{
				id: 'projects',
				type: 'action',
				action: 'projects',
				title: 'Projects',
				subtitle: 'Manage workspaces',
				icon: FolderOpen
			},
			{
				id: 'profiles',
				type: 'action',
				action: 'profiles',
				title: 'Profiles',
				subtitle: 'Agent configurations',
				icon: User
			}
		];

		if (isAdmin) {
			cards.push(
				{
					id: 'subagents',
					type: 'action',
					action: 'subagents',
					title: 'Subagents',
					subtitle: 'Manage AI subagents',
					icon: Bot
				},
				{
					id: 'settings',
					type: 'action',
					action: 'settings',
					title: 'Settings',
					subtitle: 'Preferences & config',
					icon: Settings
				}
			);
		}

		return cards;
	});

	// Build main deck cards (filtered - excludes cards in custom decks)
	const mainDeckCards = $derived.by((): NavigatorCard[] => {
		// Filter out cards that are in decks
		const filteredCards = baseActionCards.filter(card => !cardsInDecks.has(card.id));

		// Add custom deck cards
		const deckCards: NavigatorCard[] = userDecks.map(deck => ({
			id: `deck-${deck.id}`,
			type: 'deck' as const,
			deckId: deck.id,
			title: deck.name,
			subtitle: `${deck.cardIds.length} items`,
			icon: Folder,
			iconColor: deck.color,
			hasChildren: true
		}));

		// Sort by persisted order (excluding add-deck card)
		const order = navigatorState.cardOrder;
		const sortableCards = [...filteredCards, ...deckCards];

		let sortedCards: NavigatorCard[];
		if (order.length === 0) {
			sortedCards = sortableCards;
		} else {
			sortedCards = sortableCards.sort((a, b) => {
				const aIdx = order.indexOf(a.id);
				const bIdx = order.indexOf(b.id);
				// Unordered items go to the end (but before add-deck)
				if (aIdx === -1 && bIdx === -1) return 0;
				if (aIdx === -1) return 1;
				if (bIdx === -1) return -1;
				return aIdx - bIdx;
			});
		}

		// Add "New Deck" card ALWAYS at the end (only in edit mode)
		if (isEditMode) {
			sortedCards.push({
				id: 'add-deck',
				type: 'add-deck' as const,
				title: 'New Deck',
				subtitle: 'Create a group',
				icon: Plus
			});
		}

		return sortedCards;
	});

	// Build recent threads cards
	const recentThreadCards = $derived.by((): NavigatorCard[] => {
		return sessions
			.slice(0, 20)
			.map((session: Session): NavigatorCard => ({
				id: `thread-${session.id}`,
				type: 'thread',
				title: session.title || 'Untitled',
				subtitle: '',
				icon: MessageSquare,
				sessionId: session.id,
				isStreaming: isSessionStreaming(session.id),
				messageCount: session.message_count,
				lastActive: formatRelativeTime(session.updated_at)
			}));
	});

	// Build cards for a specific custom deck
	function getDeckCards(deckId: string): NavigatorCard[] {
		const deck = userDecks.find(d => d.id === deckId);
		if (!deck) return [];

		const cards: NavigatorCard[] = [];

		for (const cardId of deck.cardIds) {
			// Check if it's a thread
			if (cardId.startsWith('thread-')) {
				const sessionId = cardId.replace('thread-', '');
				const session = sessions.find((s: Session) => s.id === sessionId);
				if (session) {
					cards.push({
						id: cardId,
						type: 'thread',
						title: session.title || 'Untitled',
						subtitle: '',
						icon: MessageSquare,
						sessionId: session.id,
						isStreaming: isSessionStreaming(session.id),
						messageCount: session.message_count,
						lastActive: formatRelativeTime(session.updated_at)
					});
				}
			} else {
				// It's an action card
				const actionCard = baseActionCards.find(c => c.id === cardId);
				if (actionCard) {
					cards.push(actionCard);
				}
			}
		}

		return cards;
	}

	// Get current deck of cards based on view
	const currentCards = $derived.by((): NavigatorCard[] => {
		switch (currentView) {
			case 'recent':
				return recentThreadCards;
			case 'deck':
				return currentDeckId ? getDeckCards(currentDeckId) : [];
			default:
				return mainDeckCards;
		}
	});

	// Display cards - reordered in real-time during drag based on liveCardOrder
	const displayCards = $derived.by((): NavigatorCard[] => {
		// If not dragging or no live order, use current cards
		if (!isPointerDragging || liveCardOrder.length === 0) {
			return currentCards;
		}

		// Reorder cards based on live order (excluding dragged card and add-deck)
		const orderedCards: NavigatorCard[] = [];
		const addDeckCard = currentCards.find(c => c.type === 'add-deck');

		for (const cardId of liveCardOrder) {
			// Skip the dragged card - it's shown as the floating element
			if (cardId === draggedCardId) continue;

			const card = currentCards.find(c => c.id === cardId);
			if (card) {
				orderedCards.push(card);
			}
		}

		// Add the add-deck card at the end if it exists
		if (addDeckCard) {
			orderedCards.push(addDeckCard);
		}

		return orderedCards;
	});

	// Handle card click
	function handleCardClick(card: NavigatorCard, e: MouseEvent) {
		// Block click if we just finished dragging
		if (didDragInEditMode) {
			didDragInEditMode = false;
			return;
		}
		// Block click if in edit mode and currently dragging
		if (isEditMode && isPointerDragging) return;
		if (hasDragged) return;

		// In edit mode, show context menu on click
		if (isEditMode && card.type !== 'add-deck') {
			showContextMenu(card.id, e);
			return;
		}

		if (card.type === 'add-deck') {
			showNewDeckDialog = true;
			return;
		}

		if (card.type === 'action' && card.action) {
			handleAction(card.action);
		} else if (card.type === 'category' && card.category) {
			navigateToCategory(card);
		} else if (card.type === 'deck' && card.deckId) {
			navigateToDeck(card.deckId, card.title);
		} else if (card.type === 'thread' && card.sessionId) {
			onOpenThread(card.sessionId);
			onClose();
		}
	}

	// Handle action cards
	function handleAction(action: CardAction) {
		onClose();
		switch (action) {
			case 'new-chat': onCreateChat(); break;
			case 'terminal': onCreateTerminal(); break;
			case 'image-studio': onOpenImageStudio?.(); break;
			case 'model-studio': onOpenModelStudio?.(); break;
			case 'audio-studio': onOpenAudioStudio?.(); break;
			case 'file-browser': onOpenFileBrowser?.(); break;
			case 'projects': onOpenProjects?.(); break;
			case 'profiles': onOpenProfiles?.(); break;
			case 'subagents': onOpenSubagents?.(); break;
			case 'settings': onOpenSettings?.(); break;
		}
	}

	// Navigate to category (sub-deck)
	function navigateToCategory(card: NavigatorCard) {
		if (card.category === 'recent') {
			currentView = 'recent';
			navStack = [...navStack, { id: 'recent', name: 'Recent', type: 'category' }];
		}
		if (carouselRef) carouselRef.scrollLeft = 0;
	}

	// Navigate to custom deck
	function navigateToDeck(deckId: string, name: string) {
		currentView = 'deck';
		currentDeckId = deckId;
		navStack = [...navStack, { id: deckId, name, type: 'deck' }];
		if (carouselRef) carouselRef.scrollLeft = 0;
	}

	// Navigate back via breadcrumb
	function navigateBack(targetIndex: number) {
		if (targetIndex >= navStack.length - 1) return;

		navStack = navStack.slice(0, targetIndex + 1);
		const target = navStack[targetIndex];

		if (target.type === 'root') {
			currentView = 'main';
			currentDeckId = null;
		} else if (target.id === 'recent') {
			currentView = 'recent';
			currentDeckId = null;
		} else if (target.type === 'deck') {
			currentView = 'deck';
			currentDeckId = target.id;
		}

		if (carouselRef) carouselRef.scrollLeft = 0;
	}

	// Reset state when closing
	function handleClose() {
		if (isEditMode) {
			navigator.exitEditMode();
			isEditMode = false;
		}
		contextMenuOpen = false;
		showNewDeckDialog = false;
		onClose();
		setTimeout(() => {
			navStack = [{ id: 'root', name: 'AI Shuffle', type: 'root' }];
			currentView = 'main';
			currentDeckId = null;
			scrollProgress = 0;
		}, 300);
	}

	// Toggle edit mode
	function toggleEditMode() {
		if (isEditMode) {
			// Save card order when exiting
			const cardIds = currentCards.map(c => c.id);
			navigator.setCardOrder(cardIds);
			navigator.exitEditMode();
		} else {
			navigator.enterEditMode();
		}
		isEditMode = !isEditMode;
		contextMenuOpen = false;
	}

	// Handle keyboard
	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			if (showNewDeckDialog) {
				showNewDeckDialog = false;
			} else if (contextMenuOpen) {
				contextMenuOpen = false;
			} else if (isEditMode) {
				toggleEditMode();
			} else if (navStack.length > 1) {
				navigateBack(navStack.length - 2);
			} else {
				handleClose();
			}
		}
	}

	// ========================================
	// DRAG AND DROP FOR REORDERING
	// ========================================

	// Track if we're hovering over a deck for drop-into-deck
	let dragOverDeckId = $state<string | null>(null);

	// Pending drag state (before threshold is met)
	let pendingDragCard = $state<NavigatorCard | null>(null);
	let pendingDragIndex = $state<number>(-1);
	let pendingDragElement = $state<HTMLElement | null>(null);

	function handleDragHandlePointerDown(e: PointerEvent, card: NavigatorCard, index: number) {
		if (!isEditMode || card.type === 'add-deck') return;

		// Stop propagation so card click doesn't fire
		e.stopPropagation();
		e.preventDefault();

		const cardElement = (e.currentTarget as HTMLElement).closest('[data-card-id]') as HTMLElement;
		if (!cardElement) return;

		// Store the start position and card info
		dragStartPosition = { x: e.clientX, y: e.clientY };
		pendingDragCard = card;
		pendingDragIndex = index;
		pendingDragElement = cardElement;

		// Calculate offset from card top-left to pointer - use card element, not handle
		const rect = cardElement.getBoundingClientRect();
		pointerDragOffset = {
			x: e.clientX - rect.left,
			y: e.clientY - rect.top
		};

		// Store the carousel's Y position to lock horizontal movement
		if (carouselRef) {
			const carouselRect = carouselRef.getBoundingClientRect();
			railYPosition = rect.top; // Lock to this card's Y position
		}

		// Start long press timer for mobile
		if (isMobile) {
			longPressTimer = setTimeout(() => {
				// Mobile long-press triggers drag immediately
				startDragging(e);
			}, LONG_PRESS_DURATION);
		} else {
			// On desktop, start drag immediately from handle
			startDragging(e);
		}
	}

	// Keep the old function but make it a no-op for non-handle clicks
	function handleCardPointerDown(e: PointerEvent, card: NavigatorCard, index: number) {
		// Only allow drag initiation from the drag handle, not the card body
		// This function now does nothing - drag is handled by handleDragHandlePointerDown
		return;
	}

	function startDragging(e: PointerEvent | { clientX: number; clientY: number }) {
		if (!pendingDragCard || !pendingDragElement) return;

		draggedCardId = pendingDragCard.id;
		draggedCardIndex = pendingDragIndex;
		draggedCardElement = pendingDragElement;
		isPointerDragging = true;
		didDragInEditMode = true;

		// Initialize live card order for real-time reordering
		liveCardOrder = currentCards.filter(c => c.type !== 'add-deck').map(c => c.id);

		// Capture original card positions BEFORE any reordering happens
		// These positions stay fixed throughout the drag operation
		const cardElements = carouselRef?.querySelectorAll('[data-card-id]') as NodeListOf<HTMLElement>;
		originalCardPositions = [];
		if (cardElements) {
			for (const el of cardElements) {
				const cardId = el.dataset.cardId;
				if (!cardId) continue;
				const card = currentCards.find(c => c.id === cardId);
				if (!card || card.type === 'add-deck') continue;

				const rect = el.getBoundingClientRect();
				originalCardPositions.push({
					id: cardId,
					left: rect.left,
					width: rect.width
				});
			}
		}

		// Set initial position - lock Y to rail position
		pointerDragPosition = {
			x: e.clientX - pointerDragOffset.x,
			y: railYPosition // Lock to rail Y position
		};

		// Haptic feedback on mobile
		if (window.navigator.vibrate) {
			window.navigator.vibrate(50);
		}
	}

	function handleCardPointerUp(e: PointerEvent) {
		// Clear long press timer
		if (longPressTimer) {
			clearTimeout(longPressTimer);
			longPressTimer = null;
		}

		// Clear pending drag state
		pendingDragCard = null;
		pendingDragIndex = -1;
		pendingDragElement = null;

		// Handle the pointer-based drag end
		if (isPointerDragging) {
			handleEditPointerUp(e);
		}
	}

	function handleCardPointerMove(e: PointerEvent) {
		// Clear long press if we're moving (for mobile)
		if (longPressTimer) {
			const dx = e.clientX - dragStartPosition.x;
			const dy = e.clientY - dragStartPosition.y;
			const distance = Math.sqrt(dx * dx + dy * dy);
			if (distance > DRAG_START_THRESHOLD) {
				clearTimeout(longPressTimer);
				longPressTimer = null;
			}
		}

		// If we have a pending drag and haven't started yet, check threshold
		if (pendingDragCard && !isPointerDragging && !isMobile) {
			const dx = e.clientX - dragStartPosition.x;
			const dy = e.clientY - dragStartPosition.y;
			const distance = Math.sqrt(dx * dx + dy * dy);

			if (distance > DRAG_START_THRESHOLD) {
				// Start dragging
				startDragging(e);
			}
		}

		// Handle the pointer-based drag move
		if (isPointerDragging) {
			handleEditPointerMove(e);
		}
	}

	function handleEditPointerMove(e: PointerEvent | MouseEvent) {
		if (!isPointerDragging || !draggedCardId) return;

		// Update drag position - LOCK Y axis to rail position
		pointerDragPosition = {
			x: e.clientX - pointerDragOffset.x,
			y: railYPosition // Always keep Y locked to the rail
		};

		// Use the ORIGINAL positions captured at drag start (not current DOM positions)
		// This prevents feedback loops where positions shift as we reorder
		if (originalCardPositions.length === 0) {
			e.preventDefault();
			return;
		}

		// Find the dragged card's original position to calculate its center
		const draggedOriginal = originalCardPositions.find(p => p.id === draggedCardId);
		const draggedCardWidth = draggedOriginal?.width || 220;
		const dragCenterX = pointerDragPosition.x + draggedCardWidth / 2;

		let hoveredDeckId: string | null = null;

		// Check for deck drop-into using original positions
		const draggedCard = currentCards.find(c => c.id === draggedCardId);
		if (draggedCard?.type !== 'deck') {
			for (const pos of originalCardPositions) {
				if (pos.id === draggedCardId) continue;
				const card = currentCards.find(c => c.id === pos.id);
				if (card?.type === 'deck' && card.deckId) {
					const posRight = pos.left + pos.width;
					if (dragCenterX >= pos.left && dragCenterX <= posRight) {
						hoveredDeckId = card.deckId;
						break;
					}
				}
			}
		}

		// Calculate new position in the order using ORIGINAL positions
		if (!hoveredDeckId) {
			// Get positions of other cards (excluding the dragged one) in their original order
			const otherPositions = originalCardPositions.filter(p => p.id !== draggedCardId);

			// Find where the dragged card's center falls among the original slot positions
			// Each "slot" is defined by the center of the original card position
			let targetSlot = otherPositions.length; // Default to end

			for (let i = 0; i < otherPositions.length; i++) {
				const slotCenterX = otherPositions[i].left + otherPositions[i].width / 2;
				if (dragCenterX < slotCenterX) {
					targetSlot = i;
					break;
				}
			}

			// Build the new order: insert dragged card at targetSlot position
			const otherCardIds = otherPositions.map(p => p.id);
			const newOrder = [...otherCardIds];
			newOrder.splice(targetSlot, 0, draggedCardId);

			// Only update if order actually changed
			const orderChanged = newOrder.some((id, i) => liveCardOrder[i] !== id);
			if (orderChanged) {
				liveCardOrder = newOrder;
			}
		}

		dragOverDeckId = hoveredDeckId;

		e.preventDefault();
	}

	function handleEditPointerUp(e: PointerEvent) {
		if (!isPointerDragging || !draggedCardId) return;

		// Handle drop into deck
		if (dragOverDeckId) {
			navigator.addCardToDeck(dragOverDeckId, draggedCardId);
		}
		// Handle reorder - use the live card order that was updated during drag
		else if (liveCardOrder.length > 0) {
			navigator.setCardOrder(liveCardOrder);
		}

		// Reset drag state
		isPointerDragging = false;
		draggedCardId = null;
		draggedCardIndex = -1;
		dragOverIndex = -1;
		dragOverDeckId = null;
		draggedCardElement = null;
		liveCardOrder = [];
		originalCardPositions = [];

		// Clear pending state
		pendingDragCard = null;
		pendingDragIndex = -1;
		pendingDragElement = null;
	}

	function handleEditPointerCancel(e: PointerEvent) {
		// Clear long press timer
		if (longPressTimer) {
			clearTimeout(longPressTimer);
			longPressTimer = null;
		}

		// Clear pending state
		pendingDragCard = null;
		pendingDragIndex = -1;
		pendingDragElement = null;

		// Reset drag state without applying changes
		isPointerDragging = false;
		draggedCardId = null;
		draggedCardIndex = -1;
		dragOverIndex = -1;
		dragOverDeckId = null;
		draggedCardElement = null;
		liveCardOrder = [];
		originalCardPositions = [];
	}

	// ========================================
	// CONTEXT MENU
	// ========================================

	function showContextMenu(cardId: string, e: MouseEvent) {
		e.preventDefault();
		e.stopPropagation();
		contextMenuCardId = cardId;
		contextMenuPosition = { x: e.clientX, y: e.clientY };

		// Check if this is a deck card
		const card = currentCards.find(c => c.id === cardId);
		contextMenuIsDeck = card?.type === 'deck';

		contextMenuOpen = true;
	}

	function closeContextMenu() {
		contextMenuOpen = false;
		contextMenuCardId = null;
		contextMenuIsDeck = false;
	}

	function handleMoveToDeck(deckId: string) {
		if (!contextMenuCardId) return;
		navigator.addCardToDeck(deckId, contextMenuCardId);
		closeContextMenu();
	}

	function handleRemoveFromDeck() {
		if (!contextMenuCardId || !currentDeckId) return;
		navigator.removeCardFromDeck(currentDeckId, contextMenuCardId);
		closeContextMenu();
	}

	function handleCreateNewDeckFromMenu() {
		closeContextMenu();
		showNewDeckDialog = true;
	}

	// Get the deck ID from a deck card's context menu
	function getContextMenuDeckId(): string | null {
		if (!contextMenuCardId || !contextMenuIsDeck) return null;
		// Deck card IDs are formatted as "deck-{deckId}"
		return contextMenuCardId.replace('deck-', '');
	}

	function handleRenameDeckFromMenu() {
		const deckId = getContextMenuDeckId();
		if (!deckId) return;

		const deck = userDecks.find(d => d.id === deckId);
		if (!deck) return;

		renameDeckId = deckId;
		renameDeckName = deck.name;
		closeContextMenu();
		showRenameDeckDialog = true;
	}

	function handleDeleteDeckFromMenu() {
		const deckId = getContextMenuDeckId();
		if (!deckId) return;

		// Cards will automatically return to main view when deck is deleted
		navigator.deleteDeck(deckId);
		closeContextMenu();
	}

	// ========================================
	// DECK DIALOGS
	// ========================================

	function createNewDeck() {
		if (!newDeckName.trim()) return;
		const deckId = navigator.createDeck(newDeckName.trim());

		// If we came from context menu, add the card to the new deck
		if (contextMenuCardId && !contextMenuIsDeck) {
			navigator.addCardToDeck(deckId, contextMenuCardId);
		}

		newDeckName = '';
		showNewDeckDialog = false;
		contextMenuCardId = null;
	}

	function renameDeck() {
		if (!renameDeckId || !renameDeckName.trim()) return;
		navigator.renameDeck(renameDeckId, renameDeckName.trim());

		renameDeckId = null;
		renameDeckName = '';
		showRenameDeckDialog = false;
	}

	function handleDeleteDeck(deckId: string) {
		navigator.deleteDeck(deckId);
		if (currentDeckId === deckId) {
			navigateBack(0); // Go back to root
		}
	}

	// ========================================
	// CAROUSEL SCROLL HANDLERS
	// ========================================

	function handleMouseDown(e: MouseEvent) {
		if (isEditMode) return; // Disable scroll-drag in edit mode
		if (!carouselRef) return;
		isDragging = true;
		hasDragged = false;
		startX = e.pageX - carouselRef.offsetLeft;
		scrollLeft = carouselRef.scrollLeft;
	}

	function handleMouseMove(e: MouseEvent) {
		if (!isDragging || !carouselRef || isEditMode) return;
		const x = e.pageX - carouselRef.offsetLeft;
		const walk = (x - startX) * 1.5;

		if (Math.abs(x - startX) > DRAG_THRESHOLD) {
			hasDragged = true;
			e.preventDefault();
		}

		carouselRef.scrollLeft = scrollLeft - walk;
	}

	function handleMouseUp() {
		isDragging = false;
		setTimeout(() => { hasDragged = false; }, 50);
	}

	function handleMouseLeave() {
		isDragging = false;
		hasDragged = false;
	}

	function handleTouchStart(e: TouchEvent) {
		if (isEditMode) return;
		if (!carouselRef) return;
		isDragging = true;
		hasDragged = false;
		startX = e.touches[0].pageX - carouselRef.offsetLeft;
		scrollLeft = carouselRef.scrollLeft;
	}

	function handleTouchMove(e: TouchEvent) {
		if (!isDragging || !carouselRef || isEditMode) return;
		const x = e.touches[0].pageX - carouselRef.offsetLeft;
		const walk = (x - startX) * 1.5;

		if (Math.abs(x - startX) > DRAG_THRESHOLD) {
			hasDragged = true;
		}

		carouselRef.scrollLeft = scrollLeft - walk;
	}

	function handleTouchEnd(e: TouchEvent) {
		isDragging = false;
		// Clear long press timer if active
		if (longPressTimer) {
			clearTimeout(longPressTimer);
			longPressTimer = null;
		}
		setTimeout(() => { hasDragged = false; }, 50);
	}

	function handleWheel(e: WheelEvent) {
		if (!carouselRef) return;
		e.preventDefault();
		const scrollAmount = (e.deltaY !== 0 ? e.deltaY : e.deltaX) * 3;
		const newScrollLeft = carouselRef.scrollLeft + scrollAmount;
		const maxScroll = carouselRef.scrollWidth - carouselRef.clientWidth;
		carouselRef.scrollLeft = Math.max(0, Math.min(newScrollLeft, maxScroll));
	}

	function handleScroll() {
		if (!carouselRef) return;
		const maxScroll = carouselRef.scrollWidth - carouselRef.clientWidth;
		scrollProgress = maxScroll > 0 ? carouselRef.scrollLeft / maxScroll : 0;
	}

	const dotCount = $derived(Math.min(5, Math.ceil(currentCards.length / 3)));
	const activeDot = $derived(Math.round(scrollProgress * (dotCount - 1)));

	// Initialize navigator store
	onMount(() => {
		navigator.initialize();
	});
</script>

<svelte:window
	onkeydown={handleKeyDown}
	onpointermove={(isPointerDragging || pendingDragCard) ? handleCardPointerMove : undefined}
	onpointerup={(isPointerDragging || pendingDragCard) ? handleCardPointerUp : undefined}
/>

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div
		class="navigator"
		class:open
		class:edit-mode={isEditMode}
		class:is-dragging={isPointerDragging}
		transition:fade={{ duration: 200 }}
	>
		<!-- Backdrop -->
		<div class="navigator-backdrop" onclick={handleClose}></div>

		<!-- Header with breadcrumb -->
		<header class="navigator-header">
			<nav class="nav-breadcrumb">
				{#each navStack as item, index}
					{#if index > 0}
						<span class="breadcrumb-sep">/</span>
					{/if}
					<button
						class="breadcrumb-btn"
						class:active={index === navStack.length - 1}
						disabled={index === navStack.length - 1}
						onclick={() => navigateBack(index)}
					>
						{item.name}
					</button>
				{/each}
			</nav>

			<div class="nav-actions">
				<!-- Edit Mode Toggle -->
				{#if currentView === 'main' || currentView === 'deck'}
					<button
						class="nav-action-btn"
						class:active={isEditMode}
						onclick={toggleEditMode}
						aria-label={isEditMode ? 'Done editing' : 'Edit cards'}
						title={isEditMode ? 'Done' : 'Edit'}
					>
						{#if isEditMode}
							<Check size={18} strokeWidth={2} />
						{:else}
							<Pencil size={18} strokeWidth={2} />
						{/if}
					</button>
				{/if}

				<button class="nav-close" onclick={handleClose} aria-label="Close">
					<X size={18} strokeWidth={2} />
				</button>
			</div>
		</header>

		<!-- Edit mode hint -->
		{#if isEditMode}
			<div class="edit-mode-hint" transition:fade={{ duration: 150 }}>
				{isMobile ? 'Long press to drag • Tap for options' : 'Drag to reorder • Click for options'}
			</div>
		{/if}

		<!-- Card carousel -->
		<div class="carousel-stage">
			{#key `${currentView}-${currentDeckId}`}
				<div
					class="carousel"
					class:dragging={isDragging}
					class:edit-mode={isEditMode}
					bind:this={carouselRef}
					onmousedown={handleMouseDown}
					onmousemove={handleMouseMove}
					onmouseup={handleMouseUp}
					onmouseleave={handleMouseLeave}
					ontouchstart={handleTouchStart}
					ontouchmove={handleTouchMove}
					ontouchend={handleTouchEnd}
					onwheel={handleWheel}
					onscroll={handleScroll}
					role="listbox"
					aria-label="Card navigator"
					in:fly={{ x: 100, duration: 300, easing: cubicOut }}
					out:fly={{ x: -100, duration: 200, easing: cubicOut }}
				>
					{#each displayCards as card, index (card.id)}
						<!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
						<article
							class="card"
							class:thread-card={card.type === 'thread'}
							class:deck-card={card.type === 'deck'}
							class:add-deck-card={card.type === 'add-deck'}
							class:drop-into-deck={card.type === 'deck' && card.deckId === dragOverDeckId}
							data-ai-active={card.isStreaming}
							data-has-children={card.hasChildren}
							data-card-id={card.id}
							style:animation-delay="{index * 50}ms"
							style:--deck-color={card.iconColor}
							onclick={(e) => handleCardClick(card, e)}
							oncontextmenu={(e) => isEditMode && showContextMenu(card.id, e)}
							onkeydown={(e) => e.key === 'Enter' && handleCardClick(card, e as unknown as MouseEvent)}
							role="option"
							tabindex="0"
							aria-selected="false"
							animate:flip={{ duration: 250, easing: cubicOut }}
						>
							<div class="card-inner">
								{#if isEditMode && card.type !== 'add-deck'}
									<!-- svelte-ignore a11y_no_static_element_interactions -->
									<div
										class="drag-handle"
										onpointerdown={(e) => handleDragHandlePointerDown(e, card, index)}
									>
										<GripVertical size={16} />
									</div>
								{/if}

								{#if card.isStreaming}
									<div class="card-status">Streaming</div>
								{/if}

								<div class="card-icon" style:--icon-color={card.iconColor}>
									<card.icon size={20} strokeWidth={1.75} />
								</div>

								<h3 class="card-title">{card.title}</h3>

								{#if card.type === 'thread'}
									<div class="card-meta">
										{#if card.lastActive}
											<span class="card-meta-item">
												<Clock size={12} />
												{card.lastActive}
											</span>
										{/if}
										{#if card.messageCount}
											<span class="card-meta-item">
												<MessageSquare size={12} />
												{card.messageCount}
											</span>
										{/if}
									</div>
								{:else if card.subtitle}
									<p class="card-subtitle">{card.subtitle}</p>
								{/if}

								{#if card.hasChildren}
									<div class="card-chevron">
										<ChevronRight size={16} />
									</div>
								{/if}
							</div>
						</article>
					{/each}

					{#if currentCards.length === 0}
						<div class="empty-state">
							{#if currentView === 'deck'}
								<p>This deck is empty</p>
								<p class="empty-hint">Add cards from the main view</p>
							{:else}
								<p>No items to show</p>
							{/if}
						</div>
					{/if}
				</div>
			{/key}

			<!-- Scroll hint with dots -->
			{#if currentCards.length > 3 && !isEditMode}
				<div class="scroll-hint">
					<span class="scroll-hint-text">{isMobile ? 'Swipe' : 'Scroll'} to browse</span>
					<div class="scroll-dots">
						{#each Array(dotCount) as _, i}
							<span class="scroll-dot" class:active={i === activeDot}></span>
						{/each}
					</div>
				</div>
			{/if}
		</div>

		<!-- Floating drag clone -->
		{#if isPointerDragging && draggedCardId}
			{@const draggedCard = currentCards.find(c => c.id === draggedCardId)}
			{#if draggedCard}
				<div
					class="floating-drag-card"
					style:left="{pointerDragPosition.x}px"
					style:top="{pointerDragPosition.y}px"
					style:--deck-color={draggedCard.iconColor}
				>
					<div class="card-inner">
						<div class="card-icon" style:--icon-color={draggedCard.iconColor}>
							<draggedCard.icon size={20} strokeWidth={1.75} />
						</div>
						<h3 class="card-title">{draggedCard.title}</h3>
						{#if draggedCard.subtitle}
							<p class="card-subtitle">{draggedCard.subtitle}</p>
						{/if}
					</div>
				</div>
			{/if}
		{/if}

		<!-- Context Menu -->
		{#if contextMenuOpen}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="context-menu-backdrop" onclick={closeContextMenu}></div>
			<div
				class="context-menu"
				style:left="{contextMenuPosition.x}px"
				style:top="{contextMenuPosition.y}px"
				transition:scale={{ duration: 150, start: 0.9 }}
			>
				{#if contextMenuIsDeck}
					<!-- Deck card context menu: rename/delete -->
					<button class="context-menu-item" onclick={handleRenameDeckFromMenu}>
						<Pencil size={16} />
						Rename Deck
					</button>
					<button class="context-menu-item danger" onclick={handleDeleteDeckFromMenu}>
						<Trash2 size={16} />
						Delete Deck
					</button>
				{:else if currentView === 'deck' && currentDeckId}
					<!-- Card inside a deck: remove option -->
					<button class="context-menu-item" onclick={handleRemoveFromDeck}>
						<X size={16} />
						Remove from Deck
					</button>
				{:else}
					<!-- Regular card: move to deck options -->
					{#if userDecks.length > 0}
						<div class="context-menu-label">Move to Deck</div>
						{#each userDecks as deck}
							<button class="context-menu-item" onclick={() => handleMoveToDeck(deck.id)}>
								<Folder size={16} style="color: {deck.color}" />
								{deck.name}
							</button>
						{/each}
						<div class="context-menu-divider"></div>
					{/if}
					<button class="context-menu-item" onclick={handleCreateNewDeckFromMenu}>
						<Plus size={16} />
						New Deck...
					</button>
				{/if}
			</div>
		{/if}

		<!-- New Deck Dialog -->
		{#if showNewDeckDialog}
			<div class="dialog-backdrop" onclick={() => showNewDeckDialog = false} transition:fade={{ duration: 150 }}></div>
			<div class="dialog" transition:scale={{ duration: 200, start: 0.9 }}>
				<h2 class="dialog-title">Create New Deck</h2>
				<input
					type="text"
					class="dialog-input"
					placeholder="Deck name..."
					bind:value={newDeckName}
					onkeydown={(e) => e.key === 'Enter' && createNewDeck()}
					autofocus
				/>
				<div class="dialog-actions">
					<button class="dialog-btn cancel" onclick={() => showNewDeckDialog = false}>
						Cancel
					</button>
					<button class="dialog-btn primary" onclick={createNewDeck} disabled={!newDeckName.trim()}>
						Create
					</button>
				</div>
			</div>
		{/if}

		<!-- Rename Deck Dialog -->
		{#if showRenameDeckDialog}
			<div class="dialog-backdrop" onclick={() => showRenameDeckDialog = false} transition:fade={{ duration: 150 }}></div>
			<div class="dialog" transition:scale={{ duration: 200, start: 0.9 }}>
				<h2 class="dialog-title">Rename Deck</h2>
				<input
					type="text"
					class="dialog-input"
					placeholder="Deck name..."
					bind:value={renameDeckName}
					onkeydown={(e) => e.key === 'Enter' && renameDeck()}
					autofocus
				/>
				<div class="dialog-actions">
					<button class="dialog-btn cancel" onclick={() => showRenameDeckDialog = false}>
						Cancel
					</button>
					<button class="dialog-btn primary" onclick={renameDeck} disabled={!renameDeckName.trim()}>
						Rename
					</button>
				</div>
			</div>
		{/if}
	</div>
{/if}

<style>
	/* ========================================
	   AI SHUFFLE DESIGN SYSTEM
	   ======================================== */

	.navigator {
		--bg-deep: oklch(0.08 0.01 260);
		--bg-base: oklch(0.10 0.01 260);
		--bg-elevated: oklch(0.13 0.01 260);
		--bg-card: oklch(0.15 0.01 260);
		--bg-card-hover: oklch(0.17 0.01 260);

		--border-subtle: rgba(255, 255, 255, 0.06);
		--border-default: rgba(255, 255, 255, 0.1);
		--border-strong: rgba(255, 255, 255, 0.15);

		--text-primary: #f4f4f5;
		--text-secondary: #a1a1aa;
		--text-muted: #71717a;
		--text-dim: #52525b;

		--ai-primary: #22d3ee;
		--ai-secondary: #06b6d4;
		--ai-glow: rgba(34, 211, 238, 0.25);
		--ai-subtle: rgba(34, 211, 238, 0.08);

		--ease-out: cubic-bezier(0.16, 1, 0.3, 1);
		--ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);

		--card-width: 220px;
		--card-height: 160px;
		--card-gap: 16px;
		--radius-sm: 8px;
		--radius-md: 12px;
		--radius-lg: 16px;
	}

	/* ========================================
	   NAVIGATOR OVERLAY
	   ======================================== */
	.navigator {
		position: fixed;
		inset: 0;
		z-index: 100000;
		display: flex;
		flex-direction: column;
	}

	.navigator-backdrop {
		position: absolute;
		inset: 0;
		background: rgba(0, 0, 0, 0.85);
		backdrop-filter: blur(20px);
		-webkit-backdrop-filter: blur(20px);
	}

	/* ========================================
	   HEADER
	   ======================================== */
	.navigator-header {
		position: relative;
		z-index: 10;
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 20px 24px;
		padding-top: max(20px, env(safe-area-inset-top, 20px));
	}

	.nav-breadcrumb {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.breadcrumb-btn {
		font-family: inherit;
		font-size: 0.9375rem;
		font-weight: 500;
		color: var(--text-muted);
		background: none;
		border: none;
		cursor: pointer;
		padding: 6px 10px;
		border-radius: var(--radius-sm);
		transition: all 0.15s ease;
	}

	.breadcrumb-btn:hover:not(.active):not(:disabled) {
		color: var(--text-secondary);
		background: var(--border-subtle);
	}

	.breadcrumb-btn.active {
		color: var(--text-primary);
		cursor: default;
	}

	.breadcrumb-sep {
		color: var(--text-dim);
		font-size: 0.75rem;
	}

	.nav-actions {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.nav-action-btn {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--bg-elevated);
		border: 1px solid var(--border-default);
		border-radius: 50%;
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.nav-action-btn:hover {
		background: var(--bg-card);
		border-color: var(--border-strong);
		color: var(--text-primary);
	}

	.nav-action-btn.active {
		background: var(--ai-subtle);
		border-color: var(--ai-primary);
		color: var(--ai-primary);
	}

	.nav-close {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--bg-elevated);
		border: 1px solid var(--border-default);
		border-radius: 50%;
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.nav-close:hover {
		background: var(--bg-card);
		border-color: var(--border-strong);
		color: var(--text-primary);
	}

	/* ========================================
	   EDIT MODE HINT
	   ======================================== */
	.edit-mode-hint {
		position: relative;
		z-index: 10;
		text-align: center;
		padding: 8px 16px;
		font-size: 0.8125rem;
		color: var(--ai-primary);
		background: var(--ai-subtle);
		border-top: 1px solid rgba(34, 211, 238, 0.15);
		border-bottom: 1px solid rgba(34, 211, 238, 0.15);
	}

	/* ========================================
	   CARD CAROUSEL
	   ======================================== */
	.carousel-stage {
		flex: 1;
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		width: 100%;
		min-height: 0;
	}

	.carousel {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		gap: var(--card-gap);
		padding: 40px 60px;
		overflow-x: scroll;
		overflow-y: hidden;
		scrollbar-width: none;
		cursor: grab;
		-webkit-overflow-scrolling: touch;
	}

	.carousel::-webkit-scrollbar {
		display: none;
	}

	.carousel:active,
	.carousel.dragging {
		cursor: grabbing;
		scroll-behavior: auto;
	}

	.carousel.edit-mode {
		cursor: default;
	}

	/* Edge fade */
	.carousel-stage::before,
	.carousel-stage::after {
		content: '';
		position: absolute;
		top: 0;
		bottom: 0;
		width: 100px;
		z-index: 10;
		pointer-events: none;
	}

	.carousel-stage::before {
		left: 0;
		background: linear-gradient(90deg, var(--bg-deep) 0%, transparent 100%);
	}

	.carousel-stage::after {
		right: 0;
		background: linear-gradient(-90deg, var(--bg-deep) 0%, transparent 100%);
	}

	/* ========================================
	   CARDS
	   ======================================== */
	.card {
		flex-shrink: 0;
		width: var(--card-width);
		height: var(--card-height);
		cursor: pointer;
		transition: transform 0.2s var(--ease-out), opacity 0.2s ease;
		opacity: 0;
		transform: translateX(40px) scale(0.95);
		animation: cardEnter 0.4s var(--ease-spring) forwards;
	}

	@keyframes cardEnter {
		0% {
			opacity: 0;
			transform: translateX(40px) scale(0.95);
		}
		100% {
			opacity: 1;
			transform: translateX(0) scale(1);
		}
	}

	/* Edit mode shake animation - only when NOT dragging */
	.navigator.edit-mode:not(.is-dragging) .card:not(.add-deck-card) {
		animation: cardEnter 0.4s var(--ease-spring) forwards, cardShake 0.4s ease-in-out infinite;
		animation-delay: calc(var(--index, 0) * 50ms), 0ms;
	}

	@keyframes cardShake {
		0%, 100% { transform: rotate(0deg); }
		25% { transform: rotate(-0.5deg); }
		75% { transform: rotate(0.5deg); }
	}

	.card:hover {
		transform: translateY(-6px);
	}

	.card:active {
		transform: translateY(-4px) scale(0.98);
	}

	/* In edit mode, don't apply hover transform - let cards stay in place */
	.navigator.edit-mode .card:hover {
		transform: none;
	}

	.navigator.edit-mode .card:active {
		transform: none;
	}

	/* During drag, disable all transitions/animations so flip can work */
	.navigator.is-dragging .card {
		animation: none !important;
		transition: none !important;
	}

	/* Floating drag clone - physically picked up card */
	.floating-drag-card {
		position: fixed;
		width: var(--card-width);
		height: var(--card-height);
		z-index: 100010;
		pointer-events: none;
		transform: rotate(3deg) scale(1.08);
		animation: pickUp 0.15s var(--ease-spring) forwards;
	}

	.floating-drag-card .card-inner {
		width: 100%;
		height: 100%;
		background: var(--bg-card);
		border: 1px solid var(--ai-primary);
		border-radius: var(--radius-lg);
		padding: 20px;
		display: flex;
		flex-direction: column;
		box-shadow:
			0 20px 60px rgba(0, 0, 0, 0.6),
			0 0 30px var(--ai-glow),
			0 0 0 2px rgba(34, 211, 238, 0.3);
	}

	.floating-drag-card .card-icon {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--ai-subtle);
		border: 1px solid rgba(34, 211, 238, 0.3);
		border-radius: var(--radius-md);
		margin-bottom: 16px;
		color: var(--icon-color, var(--ai-primary));
	}

	.floating-drag-card .card-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--text-primary);
		margin-bottom: 4px;
	}

	.floating-drag-card .card-subtitle {
		font-size: 0.8125rem;
		color: var(--text-muted);
	}

	@keyframes pickUp {
		0% {
			transform: rotate(0deg) scale(1);
			box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
		}
		100% {
			transform: rotate(3deg) scale(1.08);
		}
	}

	/* Visual feedback when dropping a card INTO a deck */
	.card.drop-into-deck {
		transform: scale(1.08);
	}

	.card.drop-into-deck .card-inner {
		border-color: var(--deck-color, var(--ai-primary));
		box-shadow:
			0 0 20px color-mix(in srgb, var(--deck-color, var(--ai-primary)) 40%, transparent),
			0 8px 32px rgba(0, 0, 0, 0.4);
		background: color-mix(in srgb, var(--deck-color, var(--ai-primary)) 10%, var(--bg-card));
	}

	.card-inner {
		width: 100%;
		height: 100%;
		background: var(--bg-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-lg);
		padding: 20px;
		display: flex;
		flex-direction: column;
		position: relative;
		overflow: hidden;
		transition: all 0.2s var(--ease-out);
	}

	.card:hover .card-inner {
		background: var(--bg-card-hover);
		border-color: var(--border-strong);
		box-shadow:
			0 8px 32px rgba(0, 0, 0, 0.4),
			0 0 0 1px var(--border-subtle);
	}

	/* AI-active card glow */
	.card[data-ai-active="true"] .card-inner {
		border-color: rgba(34, 211, 238, 0.3);
		box-shadow:
			0 0 20px var(--ai-glow),
			0 8px 32px rgba(0, 0, 0, 0.3);
	}

	/* Drag handle */
	.drag-handle {
		position: absolute;
		top: 8px;
		right: 8px;
		padding: 8px;
		color: var(--text-dim);
		opacity: 0.7;
		cursor: grab;
		transition: all 0.15s ease;
		border-radius: var(--radius-sm);
		touch-action: none; /* Prevent scroll interference on touch */
		z-index: 5;
	}

	.drag-handle:hover {
		opacity: 1;
		color: var(--ai-primary);
		background: var(--ai-subtle);
	}

	.drag-handle:active {
		cursor: grabbing;
		transform: scale(0.95);
	}

	/* Card icon */
	.card-icon {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		margin-bottom: 16px;
		color: var(--icon-color, var(--text-secondary));
		transition: all 0.2s ease;
	}

	.card:hover .card-icon {
		background: var(--ai-subtle);
		border-color: rgba(34, 211, 238, 0.2);
		color: var(--icon-color, var(--ai-primary));
	}

	/* Deck card styling */
	.card.deck-card .card-icon {
		background: color-mix(in srgb, var(--deck-color, var(--ai-primary)) 15%, transparent);
		border-color: color-mix(in srgb, var(--deck-color, var(--ai-primary)) 30%, transparent);
		color: var(--deck-color, var(--ai-primary));
	}

	/* Add deck card styling */
	.card.add-deck-card .card-inner {
		border-style: dashed;
		border-color: var(--border-default);
		background: transparent;
	}

	.card.add-deck-card:hover .card-inner {
		border-color: var(--ai-primary);
		background: var(--ai-subtle);
	}

	.card.add-deck-card .card-icon {
		background: transparent;
		border-style: dashed;
	}

	.card[data-ai-active="true"] .card-icon {
		background: var(--ai-subtle);
		border-color: rgba(34, 211, 238, 0.3);
		color: var(--ai-primary);
	}

	.card[data-ai-active="true"] .card-icon :global(svg) {
		animation: pulse 2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.5; }
	}

	/* Card content */
	.card-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--text-primary);
		margin-bottom: 4px;
		line-height: 1.3;
		display: -webkit-box;
		-webkit-line-clamp: 4;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.card-subtitle {
		font-size: 0.8125rem;
		color: var(--text-muted);
		line-height: 1.4;
	}

	/* Card meta (for thread cards) */
	.card-meta {
		margin-top: auto;
		display: flex;
		align-items: center;
		gap: 12px;
		font-size: 0.75rem;
		color: var(--text-dim);
	}

	.card-meta-item {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	/* Chevron for cards with children */
	.card-chevron {
		position: absolute;
		right: 12px;
		top: 50%;
		transform: translateY(-50%);
		color: var(--text-dim);
		opacity: 0.6;
		transition: all 0.2s ease;
	}

	.card:hover .card-chevron {
		opacity: 1;
		color: var(--ai-primary);
		transform: translateY(-50%) translateX(2px);
	}

	/* Status badge */
	.card-status {
		position: absolute;
		top: 12px;
		right: 12px;
		padding: 3px 8px;
		background: var(--ai-subtle);
		border: 1px solid rgba(34, 211, 238, 0.2);
		border-radius: 4px;
		font-family: ui-monospace, monospace;
		font-size: 0.625rem;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--ai-primary);
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.card-status::before {
		content: '';
		width: 5px;
		height: 5px;
		background: var(--ai-primary);
		border-radius: 50%;
		animation: blink 1.5s ease-in-out infinite;
	}

	@keyframes blink {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.3; }
	}

	/* Thread card variant */
	.card.thread-card {
		height: 220px;
	}

	/* Empty state */
	.empty-state {
		width: 100%;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 40px;
		color: var(--text-muted);
		font-size: 0.9375rem;
		text-align: center;
	}

	.empty-hint {
		font-size: 0.8125rem;
		color: var(--text-dim);
		margin-top: 8px;
	}

	/* ========================================
	   SCROLL INDICATOR
	   ======================================== */
	.scroll-hint {
		position: absolute;
		bottom: 28px;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 18px;
		background: var(--bg-elevated);
		border: 1px solid var(--border-subtle);
		border-radius: 20px;
		z-index: 20;
	}

	.scroll-hint-text {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--text-muted);
	}

	.scroll-dots {
		display: flex;
		gap: 5px;
	}

	.scroll-dot {
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--text-dim);
		transition: all 0.2s ease;
	}

	.scroll-dot.active {
		width: 16px;
		border-radius: 3px;
		background: var(--ai-primary);
	}

	/* ========================================
	   CONTEXT MENU
	   ======================================== */
	.context-menu-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100001;
	}

	.context-menu {
		position: fixed;
		z-index: 100002;
		min-width: 180px;
		background: var(--bg-elevated);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-md);
		padding: 6px;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
	}

	.context-menu-label {
		padding: 6px 10px;
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-dim);
	}

	.context-menu-item {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 12px;
		font-size: 0.875rem;
		color: var(--text-secondary);
		background: none;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
	}

	.context-menu-item:hover {
		background: var(--bg-card);
		color: var(--text-primary);
	}

	.context-menu-item.danger {
		color: #f87171;
	}

	.context-menu-item.danger:hover {
		background: rgba(248, 113, 113, 0.1);
		color: #ef4444;
	}

	.context-menu-divider {
		height: 1px;
		background: var(--border-subtle);
		margin: 6px 0;
	}

	/* ========================================
	   DIALOG
	   ======================================== */
	.dialog-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100003;
		background: rgba(0, 0, 0, 0.5);
	}

	.dialog {
		position: fixed;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		z-index: 100004;
		width: 90%;
		max-width: 360px;
		background: var(--bg-elevated);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-lg);
		padding: 24px;
		box-shadow: 0 16px 64px rgba(0, 0, 0, 0.5);
	}

	.dialog-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--text-primary);
		margin-bottom: 16px;
	}

	.dialog-input {
		width: 100%;
		padding: 12px 14px;
		font-size: 0.9375rem;
		color: var(--text-primary);
		background: var(--bg-card);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-md);
		outline: none;
		transition: all 0.15s ease;
	}

	.dialog-input:focus {
		border-color: var(--ai-primary);
		box-shadow: 0 0 0 3px var(--ai-subtle);
	}

	.dialog-input::placeholder {
		color: var(--text-dim);
	}

	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 10px;
		margin-top: 20px;
	}

	.dialog-btn {
		padding: 10px 18px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: var(--radius-md);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.dialog-btn.cancel {
		background: none;
		border: 1px solid var(--border-default);
		color: var(--text-secondary);
	}

	.dialog-btn.cancel:hover {
		background: var(--bg-card);
		border-color: var(--border-strong);
		color: var(--text-primary);
	}

	.dialog-btn.primary {
		background: var(--ai-primary);
		border: none;
		color: #000;
	}

	.dialog-btn.primary:hover:not(:disabled) {
		background: var(--ai-secondary);
	}

	.dialog-btn.primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* ========================================
	   MOBILE RESPONSIVE
	   ======================================== */
	@media (max-width: 640px) {
		.navigator {
			--card-width: 180px;
			--card-height: 140px;
			--card-gap: 12px;
		}

		.navigator-header {
			padding: 16px 20px;
		}

		.breadcrumb-btn {
			font-size: 0.875rem;
			padding: 4px 8px;
		}

		.carousel {
			padding: 30px 40px;
		}

		.carousel-stage::before,
		.carousel-stage::after {
			width: 60px;
		}

		.card-inner {
			padding: 16px;
		}

		.card-icon {
			width: 36px;
			height: 36px;
			margin-bottom: 12px;
		}

		.card-title {
			font-size: 0.875rem;
		}

		.card-subtitle {
			font-size: 0.75rem;
		}

		.card.thread-card {
			height: 200px;
		}

		.scroll-hint {
			bottom: max(100px, calc(env(safe-area-inset-bottom) + 80px));
		}

		.edit-mode-hint {
			font-size: 0.75rem;
		}
	}

	/* Large screens */
	@media (min-width: 1200px) {
		.navigator {
			--card-width: 260px;
			--card-height: 180px;
			--card-gap: 20px;
		}

		.card.thread-card {
			height: 240px;
		}
	}

	/* ========================================
	   REDUCED MOTION
	   ======================================== */
	@media (prefers-reduced-motion: reduce) {
		*, *::before, *::after {
			animation-duration: 0.01ms !important;
			animation-iteration-count: 1 !important;
			transition-duration: 0.01ms !important;
		}
	}
</style>
