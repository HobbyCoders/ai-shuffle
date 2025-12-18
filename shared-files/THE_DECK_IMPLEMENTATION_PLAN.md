# The Deck - Complete Implementation Plan

## Overview

Transform AI Hub from a chat-only interface into a **full AI workspace** with a desktop-like environment. This plan provides step-by-step instructions for implementing "The Deck" architecture.

---

## Vision

Instead of tabs and modals, "The Deck" is a fluid **desktop-like workspace** where cards can be freely positioned, resized, snapped, minimized, and maximized - like windows on a computer desktop.

### Layout Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ [64px]           [fluid]                         [320px]        │
│ ┌─────┐ ┌─────────────────────────────────────┐ ┌─────────────┐ │
│ │     │ │  ┌──────────────┐  ┌─────────────┐  │ │ Sessions    │ │
│ │ 🖥️  │ │  │  Chat Card   │  │ Agent Card  │  │ │ ├─ Chat 1   │ │
│ │ 🤖  │ │  │  (draggable) │  │ (draggable) │  │ │ └─ Chat 2   │ │
│ │ 🎨  │ │  └──────────────┘  └─────────────┘  │ │             │ │
│ │ 📁  │ │                                     │ │ Agents      │ │
│ │     │ │     ┌─────────────────────┐         │ │ ├─ Agent 1  │ │
│ │     │ │     │   Canvas Card       │         │ │ └─ Agent 2  │ │
│ │     │ │     │   (resizable)       │         │ │             │ │
│ │ ⚙️  │ │     └─────────────────────┘         │ │ Generations │ │
│ └─────┘ └─────────────────────────────────────┘ └─────────────┘ │
│ RAIL              WORKSPACE (free-form)         CONTEXT PANEL   │
│         ┌───────────────────────────────────────────────────┐   │
│         │ [minimized cards]  │  [+Chat][+Agent][+Canvas]    │   │
│         └───────────────────────────────────────────────────┘   │
│                              DOCK [56px]                        │
└─────────────────────────────────────────────────────────────────┘
```

### Activity Modes (Rail)

1. **Workspace** - Main area where all cards live (chat, agent, canvas, terminal)
2. **Agents** - Dedicated view for launching and monitoring background agents
3. **Studio** - Direct image/video creation interface
4. **Files** - File browser (future)

### Card Behaviors (Desktop-like)

- **Free positioning** - Drag cards anywhere in workspace
- **Resizing** - Drag edges/corners to resize
- **Snapping** - Snap to edges, other cards, or grid
- **Minimize** - Collapse to dock at bottom
- **Maximize** - Expand to fill workspace
- **Close** - Remove from workspace
- **Z-order** - Click to bring to front
- **Cascade/Tile** - Quick arrange options

### Mobile Workspace (Stacked Cards)

On mobile devices (<640px), the free-form desktop doesn't work well. Instead:

```
┌─────────────────────────────┐
│ ← Chat with Claude      ┬×  │  ← Minimal header (title + close)
├─────────────────────────────┤
│                             │
│                             │
│      FULL-SCREEN CARD       │
│      (swipeable stack)      │
│                             │
│                             │
│        ← swipe →            │
├─────────────────────────────┤
│  ● ○ ○ ○                    │  ← Card indicators (dots)
├─────────────────────────────┤
│ 🖥️  🤖  🎨  📁  ⚙️           │  ← Rail (horizontal)
└─────────────────────────────┘
```

**Mobile behaviors:**
- Cards are **full-screen** and **stacked** (no free positioning)
- **Swipe left/right** to switch between cards
- **Dot indicators** show current position in stack
- No minimize (cards are either open or closed)
- No resize handles
- Swipe down or tap close to dismiss card
- Context panel becomes a **slide-up sheet** (triggered by button)

### Creating New Cards

**Removed:** Quick action buttons (+Chat, +Agent, +Canvas) from dock.

**New approach - Spotlight/Command Palette (Cmd+K):**

The existing Spotlight search becomes the universal launcher:

```
┌─────────────────────────────────────────┐
│ 🔍 Type to search or create...          │
├─────────────────────────────────────────┤
│ QUICK ACTIONS                           │
│   + New Chat                     ⌘N     │
│   + New Agent Task               ⌘⇧A    │
│   + New Canvas                   ⌘⇧C    │
│   + New Terminal                 ⌘⇧T    │
├─────────────────────────────────────────┤
│ RECENT SESSIONS                         │
│   💬 Chat with Claude about...          │
│   💬 Debugging the API endpoint         │
├─────────────────────────────────────────┤
│ COMMANDS                                │
│   /context - Show context usage         │
│   /clear - Clear messages               │
└─────────────────────────────────────────┘
```

**Additional entry points:**
- **Context Panel** - Click session/agent to open as card
- **Keyboard shortcuts** - Cmd+N (chat), Cmd+Shift+A (agent), etc.
- **Empty workspace** - Shows welcome screen with create buttons
- **Right-click workspace** - Context menu with create options (desktop only)

---

## Phase 1: Core Layout Foundation

### Step 1.1: Create Types

**File:** `frontend/src/lib/components/deck/types.ts`

```typescript
export type ActivityMode = 'workspace' | 'agents' | 'studio' | 'files';

export interface ActivityBadges {
  workspace?: { count?: number; dot?: boolean; variant?: 'default' | 'warning' | 'error' | 'success' };
  agents?: { count?: number; dot?: boolean; variant?: 'default' | 'warning' | 'error' | 'success' };
  studio?: { count?: number; dot?: boolean; variant?: 'default' | 'warning' | 'error' | 'success' };
  files?: { count?: number; dot?: boolean; variant?: 'default' | 'warning' | 'error' | 'success' };
}

export interface DeckSession {
  id: string;
  title: string;
  preview?: string;
  updatedAt: Date;
  tokenCount?: number;
  cost?: number;
  active?: boolean;
}

export interface DeckAgent {
  id: string;
  name: string;
  status: 'running' | 'paused' | 'completed' | 'failed';
  progress?: number;
  branch?: string;
  startedAt: Date;
}

export interface DeckGeneration {
  id: string;
  type: 'image' | 'video';
  prompt: string;
  status: 'pending' | 'generating' | 'completed' | 'failed';
  progress?: number;
  thumbnailUrl?: string;
  resultUrl?: string;
}

export interface SessionInfo {
  tokensIn: number;
  tokensOut: number;
  cost: number;
  model?: string;
}

// Card position and size for free-form layout
export interface CardGeometry {
  x: number;
  y: number;
  width: number;
  height: number;
  zIndex: number;
}

export interface CardState {
  minimized: boolean;
  maximized: boolean;
  snappedTo?: 'left' | 'right' | 'top' | 'bottom' | 'topleft' | 'topright' | 'bottomleft' | 'bottomright';
}
```

### Step 1.2: Create DeckLayout Component

**File:** `frontend/src/lib/components/deck/DeckLayout.svelte`

Key requirements:
- CSS Grid layout with 3 areas: rail, workspace, context
- Workspace area is the canvas for free-form cards
- Dock at bottom for minimized cards and quick actions
- Responsive: on mobile (<640px), rail moves to bottom
- Context panel collapsible

```css
.deck-layout {
  display: grid;
  grid-template-columns: 64px 1fr 320px;
  grid-template-rows: 1fr 56px;
  grid-template-areas:
    "rail workspace context"
    "rail dock dock";
  height: 100vh;
  overflow: hidden;
}

.deck-layout.context-collapsed {
  grid-template-columns: 64px 1fr 0;
}

.workspace {
  grid-area: workspace;
  position: relative; /* Cards positioned absolutely within */
  overflow: hidden;
  background: var(--background);
}

/* Mobile - No dock, stacked cards */
@media (max-width: 640px) {
  .deck-layout {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr auto 56px;
    grid-template-areas:
      "workspace"
      "indicators"
      "rail";
  }

  .dock {
    display: none; /* No dock on mobile */
  }

  .workspace {
    /* Full-screen stacked cards handled by MobileWorkspace component */
  }
}
```

Props:
- `activeMode: ActivityMode`
- `badges: ActivityBadges`
- `contextCollapsed: boolean`
- `sessions: DeckSession[]`
- `agents: DeckAgent[]`
- `generations: DeckGeneration[]`
- `currentSession: SessionInfo | null`
- Event handlers for mode change, settings click, session/agent/generation clicks, new chat/agent/create

### Step 1.3: Create ActivityRail Component

**File:** `frontend/src/lib/components/deck/ActivityRail.svelte`

Requirements:
- Logo button at top (lightning icon)
- 4 activity buttons: **Workspace** (monitor icon), Agents, Studio, Files
- Settings button at bottom
- Active state: 3px colored bar on left side with glow
- Badge support: count or dot indicator
- Tooltips on hover
- Mobile: horizontal layout

### Step 1.4: Create ContextPanel Component

**File:** `frontend/src/lib/components/deck/ContextPanel.svelte`

Requirements:
- Collapse toggle button (chevron)
- 4 collapsible sections:
  1. Active Threads - list of sessions (click to open as card)
  2. Running Agents - agents with status dots
  3. Generations - media with thumbnails
  4. Session Info - tokens, cost, model (for focused card)
- Glassmorphism background (`bg-card/80 backdrop-blur-xl`)

### Step 1.5: Create Dock Component

**File:** `frontend/src/lib/components/deck/Dock.svelte`

Requirements:
- **Left section:** Minimized cards (click to restore)
  - Show card type icon + title (truncated)
  - Hover to preview thumbnail
  - Click to restore
  - Right-click for context menu (close, restore, maximize)
- **Center section:** Running processes
  - Active generations with progress rings
  - Running agents with status dots
- **Right section:** Empty (quick actions moved to Spotlight)
- Tooltips on all interactive elements
- **Desktop only** - Hidden on mobile (cards don't minimize on mobile)

### Step 1.6: Create Index Export

**File:** `frontend/src/lib/components/deck/index.ts`

```typescript
export { default as DeckLayout } from './DeckLayout.svelte';
export { default as ActivityRail } from './ActivityRail.svelte';
export { default as ContextPanel } from './ContextPanel.svelte';
export { default as Dock } from './Dock.svelte';
export * from './types';
```

### Verification Checkpoint 1

```bash
cd frontend && npm run build && npm run check
```

Expected: 0 errors (warnings OK)

---

## Phase 2: Card System (Desktop-like Windows)

### Step 2.1: Create Card Types

**File:** `frontend/src/lib/components/deck/cards/types.ts`

```typescript
export type CardType = 'chat' | 'agent' | 'canvas' | 'terminal';

export interface DeckCard {
  id: string;
  type: CardType;
  title: string;

  // Geometry (position & size)
  x: number;
  y: number;
  width: number;
  height: number;
  zIndex: number;

  // State
  minimized: boolean;
  maximized: boolean;
  focused: boolean;

  // Snap state
  snappedTo?: 'left' | 'right' | 'top' | 'bottom' | 'topleft' | 'topright' | 'bottomleft' | 'bottomright';

  // Restore geometry (for unmaximize/unsnap)
  restoreGeometry?: { x: number; y: number; width: number; height: number };

  // Card-specific data
  data?: any;

  // Timestamps
  createdAt?: Date;
  updatedAt?: Date;
}

// Default sizes for each card type
export const DEFAULT_CARD_SIZES: Record<CardType, { width: number; height: number }> = {
  chat: { width: 480, height: 640 },
  agent: { width: 400, height: 500 },
  canvas: { width: 600, height: 500 },
  terminal: { width: 600, height: 400 },
};

// Minimum sizes
export const MIN_CARD_SIZES: Record<CardType, { width: number; height: number }> = {
  chat: { width: 360, height: 400 },
  agent: { width: 320, height: 300 },
  canvas: { width: 400, height: 300 },
  terminal: { width: 400, height: 200 },
};

// Snap threshold (pixels)
export const SNAP_THRESHOLD = 20;
export const SNAP_GRID = 10; // Optional grid snapping
```

### Step 2.2: Create BaseCard Component (Draggable Window)

**File:** `frontend/src/lib/components/deck/cards/BaseCard.svelte`

This is a draggable, resizable window component.

Requirements:
- **Header (Title Bar)**:
  - Drag handle (entire header)
  - Card type icon
  - Title (editable on double-click for chat cards)
  - Window controls: minimize, maximize/restore, close
- **Content area** (slot/snippet)
- **Footer area** (optional slot/snippet)
- **Resize handles**: 8 handles (4 corners + 4 edges)
- **States**:
  - Focused: elevated shadow, brighter border
  - Unfocused: dimmed
  - Minimized: hidden (shown in dock)
  - Maximized: fills workspace
  - Snapped: fills half/quarter of workspace
- **Glassmorphism**: `bg-card/80 backdrop-blur-xl`
- **Animations**: smooth transitions for maximize/minimize/snap

```svelte
<!-- BaseCard.svelte structure -->
<script lang="ts">
  import type { DeckCard } from './types';

  let { card, onClose, onMinimize, onMaximize, onFocus, onMove, onResize, children } = $props();

  let isDragging = $state(false);
  let isResizing = $state(false);
  let resizeDirection = $state<string | null>(null);

  // Drag handlers
  function handleDragStart(e: PointerEvent) { ... }
  function handleDrag(e: PointerEvent) { ... }
  function handleDragEnd(e: PointerEvent) { ... }

  // Resize handlers
  function handleResizeStart(e: PointerEvent, direction: string) { ... }
  function handleResize(e: PointerEvent) { ... }
  function handleResizeEnd(e: PointerEvent) { ... }

  // Double-click title bar to maximize/restore
  function handleTitleBarDoubleClick() { ... }
</script>

<div
  class="card"
  class:focused={card.focused}
  class:maximized={card.maximized}
  class:minimized={card.minimized}
  style:left="{card.x}px"
  style:top="{card.y}px"
  style:width="{card.width}px"
  style:height="{card.height}px"
  style:z-index={card.zIndex}
  onpointerdown={onFocus}
>
  <!-- Header / Title Bar -->
  <header onpointerdown={handleDragStart} ondblclick={handleTitleBarDoubleClick}>
    <span class="card-icon">{typeIcon}</span>
    <span class="card-title">{card.title}</span>
    <div class="window-controls">
      <button onclick={onMinimize}>─</button>
      <button onclick={onMaximize}>{card.maximized ? '❐' : '□'}</button>
      <button onclick={onClose}>×</button>
    </div>
  </header>

  <!-- Content -->
  <main>
    {@render children()}
  </main>

  <!-- Resize Handles (when not maximized) -->
  {#if !card.maximized}
    <div class="resize-handle n" onpointerdown={(e) => handleResizeStart(e, 'n')} />
    <div class="resize-handle s" onpointerdown={(e) => handleResizeStart(e, 's')} />
    <div class="resize-handle e" onpointerdown={(e) => handleResizeStart(e, 'e')} />
    <div class="resize-handle w" onpointerdown={(e) => handleResizeStart(e, 'w')} />
    <div class="resize-handle ne" onpointerdown={(e) => handleResizeStart(e, 'ne')} />
    <div class="resize-handle nw" onpointerdown={(e) => handleResizeStart(e, 'nw')} />
    <div class="resize-handle se" onpointerdown={(e) => handleResizeStart(e, 'se')} />
    <div class="resize-handle sw" onpointerdown={(e) => handleResizeStart(e, 'sw')} />
  {/if}
</div>
```

### Step 2.3: Create Workspace Component (Card Canvas - Desktop)

**File:** `frontend/src/lib/components/deck/cards/Workspace.svelte`

This is the canvas where cards live on desktop.

Requirements:
- Contains all visible (non-minimized) cards
- Handles snapping logic when cards are dragged near edges
- Shows snap preview guides
- Tracks workspace bounds for card positioning
- Provides cascade/tile actions
- Context menu on workspace background for quick actions
- **Empty state**: Welcome message + create buttons when no cards open

```typescript
// Snap zones (visualized when dragging near edges)
// Left half, right half, top half, bottom half
// Top-left quarter, top-right quarter, bottom-left quarter, bottom-right quarter
// Full screen (when dragged to top edge)

interface SnapZone {
  id: string;
  bounds: { x: number; y: number; width: number; height: number };
  activationArea: { x: number; y: number; width: number; height: number };
}
```

### Step 2.3b: Create MobileWorkspace Component (Stacked Cards - Mobile)

**File:** `frontend/src/lib/components/deck/cards/MobileWorkspace.svelte`

Full-screen stacked card view for mobile devices.

Requirements:
- **Swipeable card stack** using touch gestures
  - Swipe left/right to navigate between cards
  - Velocity-based animation (flick to switch faster)
  - Rubber-band effect at edges
- **Card indicators** (dots) showing position in stack
  - Tappable to jump to specific card
  - Current card highlighted
- **Card header** (minimal)
  - Back arrow (returns to empty state / card list)
  - Card title (truncated)
  - Close button
- **Empty state** when no cards
  - "No open cards" message
  - Large create buttons: New Chat, New Agent, New Canvas
- **Gesture handling**
  - Swipe down to close card (optional)
  - Long press for card options menu
- Use CSS `scroll-snap` or gesture library (e.g., `use-gesture`)

```svelte
<!-- MobileWorkspace.svelte structure -->
<script lang="ts">
  let { cards, activeCardIndex, onCardChange, onCloseCard, onCreateCard } = $props();

  let touchStartX = $state(0);
  let touchDeltaX = $state(0);
  let isDragging = $state(false);

  function handleTouchStart(e: TouchEvent) { ... }
  function handleTouchMove(e: TouchEvent) { ... }
  function handleTouchEnd(e: TouchEvent) { ... }
</script>

{#if cards.length === 0}
  <!-- Empty state with create buttons -->
  <div class="empty-state">
    <h2>No open cards</h2>
    <div class="create-buttons">
      <button onclick={() => onCreateCard('chat')}>+ New Chat</button>
      <button onclick={() => onCreateCard('agent')}>+ New Agent</button>
      <button onclick={() => onCreateCard('canvas')}>+ New Canvas</button>
    </div>
  </div>
{:else}
  <!-- Card stack -->
  <div
    class="card-stack"
    ontouchstart={handleTouchStart}
    ontouchmove={handleTouchMove}
    ontouchend={handleTouchEnd}
    style:transform="translateX({-activeCardIndex * 100 + touchDeltaX}%)"
  >
    {#each cards as card, i (card.id)}
      <div class="mobile-card" class:active={i === activeCardIndex}>
        <!-- Card header -->
        <header>
          <button onclick={() => onCardChange(-1)}>←</button>
          <span>{card.title}</span>
          <button onclick={() => onCloseCard(card.id)}>×</button>
        </header>
        <!-- Card content (full height) -->
        <main>
          {#if card.type === 'chat'}
            <ChatCard {card} tabId={card.data.tabId} mobile />
          {:else if card.type === 'agent'}
            <AgentCard {card} mobile />
          {:else if card.type === 'canvas'}
            <CanvasCard {card} mobile />
          {/if}
        </main>
      </div>
    {/each}
  </div>

  <!-- Dot indicators -->
  <div class="card-indicators">
    {#each cards as card, i (card.id)}
      <button
        class="dot"
        class:active={i === activeCardIndex}
        onclick={() => onCardChange(i)}
      />
    {/each}
  </div>
{/if}
```

### Step 2.4: Create ChatCard Component (EXACT REPLICA OF MAIN CHAT)

**File:** `frontend/src/lib/components/deck/cards/ChatCard.svelte`

**CRITICAL: This must be a pixel-perfect functional replica of the existing chat UI.**

The ChatCard wraps BaseCard and provides the full chat experience inside.

#### ChatCard Structure

```
┌─────────────────────────────────────────┐
│ [Header - condensed version]            │
│ Context % │ Profile ▼ │ Project ▼ │ ─□× │
├─────────────────────────────────────────┤
│                                         │
│           MESSAGE AREA                  │
│    (scrollable, auto-scroll)            │
│                                         │
│  - User messages                        │
│  - Assistant messages (markdown)        │
│  - Tool use (collapsible)               │
│  - Streaming indicator                  │
│                                         │
├─────────────────────────────────────────┤
│ [Permission/Question Banners if any]    │
├─────────────────────────────────────────┤
│ [Input Area]                            │
│ Model ▼ │ Mode ▼ │ Reset │ Attach      │
│ ┌─────────────────────────────────────┐ │
│ │ [Attached files chips]              │ │
│ │ Message Claude...              🎤 ➤ │ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

#### Required Features (ALL MUST WORK)

**Header Section:**
- Context usage indicator (circular progress, color-coded, tooltip with breakdown)
- Profile selector dropdown (grouped, with create/manage option, locked variant for API users)
- Project selector dropdown (grouped, with create/manage option, locked when session active)
- Window controls integrated into card header

**Message Area:**
- User messages with "You" label, timestamp
- Assistant messages with "Claude" label, timestamp, markdown rendering, syntax highlighting
- Copy button on assistant messages (shows "Copied" feedback)
- Fork button on assistant messages (creates branch from that point)
- Tool use messages (collapsible, status icons: spinner/checkmark/error)
- Tool result messages
- Streaming indicator (bouncing dots)
- Streaming cursor (blinking)
- Auto-scroll behavior (scroll to bottom on new messages, pause when user scrolls up)
- Media handling: images with download/copy, videos with controls, file download cards
- TodoList rendering for TodoWrite tool
- Subagent message collapsible sections
- System messages

**Input Section (Condensed Layout):**
- Top row pills (admin only):
  - Model override: Sonnet/Sonnet 1M/Opus/Haiku/Inherit
  - Permission mode override: Ask/Auto-Accept/Plan/Bypass
  - Reset button (when overrides active)
  - Attach file button (disabled when no project)
- Uploaded files display (chips with remove button)
- Main input:
  - Auto-growing textarea (44px min, 200px max)
  - Command autocomplete (/) with keyboard navigation
  - File autocomplete (@) with directory navigation
  - Voice recording button (states: normal, recording, transcribing)
  - Send/Stop button (send arrow or stop square during streaming)

**Queues & Banners:**
- Permission request queue (Allow/Deny/Remember)
- User question queue (form inputs)

**Store Connections:**
- Use existing `tabs` store
- Tab data: `$allTabs.find(t => t.id === tabId)`
- Send message: `tabs.sendMessage(tabId, message, files)`
- Stop streaming: `tabs.stopStreaming(tabId)`
- Fork: `tabs.forkFromMessage(tabId, messageIndex)`

#### Implementation Strategy

**DO NOT create simplified versions of these components.** Instead:

1. **Extract existing chat UI into reusable components** from `+page.svelte`
2. **ChatCard imports and uses these same components**
3. **Pass `tabId` prop** to identify which tab's data to display/modify

Create these sub-components by extracting from existing code:

**Files in `frontend/src/lib/components/chat/`:** (shared between page and card)

1. **ChatHeader.svelte** - Context indicator + Profile selector + Project selector
2. **MessageArea.svelte** - Scrollable message container with auto-scroll
3. **UserMessage.svelte** - User message bubble
4. **AssistantMessage.svelte** - Assistant message with markdown, copy, fork
5. **ToolUseMessage.svelte** - Collapsible tool execution
6. **ToolResultMessage.svelte** - Tool result display
7. **StreamingIndicator.svelte** - Bouncing dots
8. **ChatInput.svelte** - Full input section with all features
9. **PermissionBanner.svelte** - Permission request UI
10. **QuestionBanner.svelte** - User question UI
11. **CommandAutocomplete.svelte** - / command menu
12. **FileAutocomplete.svelte** - @ file picker
13. **index.ts** - Export all

**ChatCard.svelte** then simply:
```svelte
<script lang="ts">
  import { ChatHeader, MessageArea, ChatInput, PermissionBanner, QuestionBanner } from '$lib/components/chat';
  import BaseCard from './BaseCard.svelte';

  let { card, tabId, onClose, onMinimize, onMaximize, onFocus, onMove, onResize } = $props();
</script>

<BaseCard {card} {onClose} {onMinimize} {onMaximize} {onFocus} {onMove} {onResize}>
  {#snippet header()}
    <ChatHeader {tabId} compact />
  {/snippet}

  <div class="chat-card-content">
    <MessageArea {tabId} />
    <PermissionBanner {tabId} />
    <QuestionBanner {tabId} />
    <ChatInput {tabId} compact />
  </div>
</BaseCard>
```

### Step 2.5: Create AgentCard Component

**File:** `frontend/src/lib/components/deck/cards/AgentCard.svelte`

Requirements:
- Status header with duration timer
- Branch badge with GitHub link
- Mini task tree (collapsible)
- Last few log lines (scrollable)
- Control buttons: Pause, Resume, Intervene, Cancel
- Progress bar

### Step 2.6: Create CanvasCard Component

**File:** `frontend/src/lib/components/deck/cards/CanvasCard.svelte`

Requirements:
- Large preview area (image/video display)
- Prompt input
- Provider selector dropdown
- Aspect ratio selector
- Generate button
- Variations thumbnails (horizontal scroll)
- Edit/extend actions

### Step 2.7: Create TerminalCard Component

**File:** `frontend/src/lib/components/deck/cards/TerminalCard.svelte`

Requirements:
- xterm.js embed (dynamic import for SSR safety)
- Clear and copy buttons
- Connection status indicator
- Resize handling (xterm needs to be told dimensions)

### Step 2.8: Create Card Index

**File:** `frontend/src/lib/components/deck/cards/index.ts`

Export all card components and types.

### Verification Checkpoint 2

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 3: State Management

### Step 3.1: Create Deck Store (Desktop Window Manager)

**File:** `frontend/src/lib/stores/deck.ts`

```typescript
interface DeckState {
  activeMode: ActivityMode;
  cards: DeckCard[];
  focusedCardId: string | null;
  nextZIndex: number;
  contextPanelCollapsed: boolean;

  // Workspace bounds (updated on resize)
  workspaceBounds: { width: number; height: number };

  // Snap settings (desktop only)
  snapEnabled: boolean;
  gridSnapEnabled: boolean;

  // Mobile state
  isMobile: boolean;
  mobileActiveCardIndex: number; // Which card is currently shown on mobile
}

// Initial state
const initialState: DeckState = {
  activeMode: 'workspace',
  cards: [],
  focusedCardId: null,
  nextZIndex: 1,
  contextPanelCollapsed: false,
  workspaceBounds: { width: 0, height: 0 },
  snapEnabled: true,
  gridSnapEnabled: false,
  isMobile: false,
  mobileActiveCardIndex: 0,
};
```

**Actions:**

```typescript
// Mode
setMode(mode: ActivityMode): void;

// Card lifecycle
addCard(type: CardType, data?: any): string; // Returns card ID
removeCard(id: string): void;
duplicateCard(id: string): string;

// Focus & Z-order
focusCard(id: string): void; // Brings to front, updates zIndex
blurCard(id: string): void;

// Position & Size
moveCard(id: string, x: number, y: number): void;
resizeCard(id: string, width: number, height: number): void;
setCardGeometry(id: string, geometry: Partial<CardGeometry>): void;

// Window states
minimizeCard(id: string): void;
restoreCard(id: string): void;
maximizeCard(id: string): void;
unmaximizeCard(id: string): void;
toggleMaximize(id: string): void;

// Snapping
snapCard(id: string, zone: SnapZone): void;
unsnapCard(id: string): void;

// Bulk operations
cascadeCards(): void; // Arrange in diagonal cascade
tileCards(): void; // Arrange in grid
minimizeAll(): void;
closeAll(): void;

// Settings
setWorkspaceBounds(width: number, height: number): void;
toggleSnapEnabled(): void;
toggleGridSnap(): void;
toggleContextPanel(): void;

// Mobile
setIsMobile(isMobile: boolean): void;
setMobileActiveCardIndex(index: number): void;
mobileNextCard(): void;
mobilePrevCard(): void;
```

**Derived stores:**
- `visibleCards` - cards where `minimized === false`
- `minimizedCards` - cards where `minimized === true`
- `focusedCard` - the currently focused card
- `chatCards` - cards where `type === 'chat'`
- `agentCards` - cards where `type === 'agent'`

**Persistence:** localStorage with debounced saves (500ms)
- Save: card positions, sizes, minimized/maximized states
- Restore: on page load, restore card layout

### Step 3.2: Create Agents Store

**File:** `frontend/src/lib/stores/agents.ts`

```typescript
interface BackgroundAgent {
  id: string;
  name: string;
  prompt: string;
  status: 'queued' | 'running' | 'paused' | 'completed' | 'failed';
  progress: number;
  branch?: string;
  tasks: AgentTask[];
  logs: string[];
  startedAt: Date;
  completedAt?: Date;
  error?: string;
}

interface AgentTask {
  id: string;
  name: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  children?: AgentTask[];
}
```

Actions:
- `launchAgent(name, prompt, options)`
- `pauseAgent(id)`, `resumeAgent(id)`, `cancelAgent(id)`
- `interveneAgent(id)` - opens chat with agent context
- `appendLog(id, line)`, `updateTask(agentId, taskId, updates)`

WebSocket: Connect to `/api/v1/agents/ws` for real-time updates

### Step 3.3: Create Studio Store

**File:** `frontend/src/lib/stores/studio.ts`

```typescript
interface StudioState {
  activeGeneration: DeckGeneration | null;
  imageProvider: 'google-gemini' | 'google-imagen' | 'openai-gpt-image';
  videoProvider: 'google-veo' | 'openai-sora';
  defaultAspectRatio: string;
  recentGenerations: DeckGeneration[];
  savedAssets: Asset[];
}

interface Asset {
  id: string;
  type: 'image' | 'video';
  url: string;
  prompt: string;
  provider: string;
  createdAt: Date;
  tags: string[];
}
```

Actions:
- `generateImage(prompt, options)`, `generateVideo(prompt, options)`
- `editImage(assetId, prompt)`, `extendVideo(assetId, prompt)`
- `saveAsset(generation)`, `deleteAsset(id)`
- `setProvider(type, provider)`

### Step 3.4: Create Store Index

**File:** `frontend/src/lib/stores/index.ts`

Export all new stores alongside existing ones. Handle naming conflicts carefully.

### Verification Checkpoint 3

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 4: Extract Chat Components (CRITICAL)

**This phase extracts the existing chat UI into reusable components.**

### Step 4.1: Create Chat Components Directory

```bash
mkdir -p frontend/src/lib/components/chat
```

### Step 4.2: Extract ChatHeader

**File:** `frontend/src/lib/components/chat/ChatHeader.svelte`

Extract from `+page.svelte`:
- Context usage indicator (the circular percentage pill)
- Profile selector dropdown (with groups, create/manage)
- Project selector dropdown (with groups, create/manage, lock when session exists)

Props:
- `tabId: string`
- `compact?: boolean` (for card mode - horizontal layout)

### Step 4.3: Extract MessageArea

**File:** `frontend/src/lib/components/chat/MessageArea.svelte`

Extract the scrollable message container including:
- Auto-scroll behavior (via action)
- Empty state
- All message type rendering

Props:
- `tabId: string`

### Step 4.4: Extract Message Components

**Files:**
- `UserMessage.svelte` - User message with label, timestamp
- `AssistantMessage.svelte` - With markdown, copy, fork buttons
- `ToolUseMessage.svelte` - Collapsible with status
- `ToolResultMessage.svelte` - Result display
- `StreamingIndicator.svelte` - Bouncing dots
- `SystemMessage.svelte` - System notifications
- `SubagentMessage.svelte` - Collapsible subagent work

### Step 4.5: Extract ChatInput

**File:** `frontend/src/lib/components/chat/ChatInput.svelte`

Extract the full input section:
- Model/mode override pills (admin)
- Reset button
- Attach button
- Uploaded files display
- Textarea with auto-grow
- Command autocomplete (/)
- File autocomplete (@)
- Voice recording button
- Send/Stop button

Props:
- `tabId: string`
- `compact?: boolean` (for card mode)

### Step 4.6: Extract Autocomplete Components

**Files:**
- `CommandAutocomplete.svelte` - / commands
- `FileAutocomplete.svelte` - @ files

### Step 4.7: Extract Banner Components

**Files:**
- `PermissionBanner.svelte` - Permission request queue
- `QuestionBanner.svelte` - User question queue

### Step 4.8: Create Chat Index

**File:** `frontend/src/lib/components/chat/index.ts`

```typescript
export { default as ChatHeader } from './ChatHeader.svelte';
export { default as MessageArea } from './MessageArea.svelte';
export { default as UserMessage } from './UserMessage.svelte';
export { default as AssistantMessage } from './AssistantMessage.svelte';
export { default as ToolUseMessage } from './ToolUseMessage.svelte';
export { default as ToolResultMessage } from './ToolResultMessage.svelte';
export { default as StreamingIndicator } from './StreamingIndicator.svelte';
export { default as SystemMessage } from './SystemMessage.svelte';
export { default as SubagentMessage } from './SubagentMessage.svelte';
export { default as ChatInput } from './ChatInput.svelte';
export { default as CommandAutocomplete } from './CommandAutocomplete.svelte';
export { default as FileAutocomplete } from './FileAutocomplete.svelte';
export { default as PermissionBanner } from './PermissionBanner.svelte';
export { default as QuestionBanner } from './QuestionBanner.svelte';
```

### Step 4.9: Update +page.svelte to Use Extracted Components

After extraction, `+page.svelte` should import and use these components instead of inline code. This validates the extraction works before using in ChatCard.

### Verification Checkpoint 4

```bash
cd frontend && npm run build && npm run check
```

**Test:** The main page should function identically after extraction.

---

## Phase 5: Agent Mode Components

### Step 5.1: Create AgentsView

**File:** `frontend/src/lib/components/deck/agents/AgentsView.svelte`

Requirements:
- Header with "Launch Agent" button
- Tabs: Running, Queued, Completed, Failed, Stats
- List of AgentListItem components
- Empty states for each tab

### Step 5.2: Create AgentLauncher

**File:** `frontend/src/lib/components/deck/agents/AgentLauncher.svelte`

Requirements:
- Modal/panel design
- Task name input
- Large prompt textarea
- Options: auto-branch, auto-PR, max duration
- Profile and project selectors
- Launch button

### Step 5.3: Create AgentListItem

**File:** `frontend/src/lib/components/deck/agents/AgentListItem.svelte`

Requirements:
- Status indicator (colored dot, animated for running)
- Agent name and status text
- Progress bar
- Duration badge
- Quick actions: Pause, View, Cancel

### Step 5.4: Create AgentDetails

**File:** `frontend/src/lib/components/deck/agents/AgentDetails.svelte`

Requirements:
- Full status header with metadata
- Branch info with GitHub link
- Tabs: Tasks (with TaskTree) and Logs
- Control buttons

### Step 5.5: Create AgentLogs

**File:** `frontend/src/lib/components/deck/agents/AgentLogs.svelte`

Requirements:
- Virtual scroll for performance
- Search within logs
- Log level filtering (all, info, warn, error)
- Timestamp toggle
- Auto-scroll toggle
- Copy all button

### Step 5.6: Create TaskTree

**File:** `frontend/src/lib/components/deck/agents/TaskTree.svelte`

Requirements:
- Recursive tree structure
- Status icons: ✓ (completed), ⟳ (running), ○ (pending), ✗ (failed)
- Expandable/collapsible branches
- Progress percentage
- CSS-based connector lines

### Step 5.7: Create AgentStats

**File:** `frontend/src/lib/components/deck/agents/AgentStats.svelte`

Requirements:
- Time filter (today/week/month/all)
- Stats: total agents, success rate, avg completion time
- Activity chart (7-day bar chart)

### Step 5.8: Create Agents Index

**File:** `frontend/src/lib/components/deck/agents/index.ts`

### Verification Checkpoint 5

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 6: Studio Mode Components

### Step 6.1: Create StudioView

**File:** `frontend/src/lib/components/deck/studio/StudioView.svelte`

Requirements:
- Split layout: 60% preview, 40% controls
- Tabs: Image, Video
- Shows active generation or empty state
- Asset library toggle

### Step 6.2: Create ImageGenerator

**File:** `frontend/src/lib/components/deck/studio/ImageGenerator.svelte`

Requirements:
- Large prompt textarea
- Provider selector with descriptions:
  - Nano Banana (google-gemini) - "Fast, supports editing"
  - Imagen 4 (google-imagen) - "Highest quality"
  - GPT Image (openai-gpt-image) - "Best text rendering"
- Aspect ratio buttons: 1:1, 16:9, 9:16, 4:3
- Style presets dropdown
- Generate button

### Step 6.3: Create VideoGenerator

**File:** `frontend/src/lib/components/deck/studio/VideoGenerator.svelte`

Requirements:
- Prompt textarea
- Provider selector:
  - Veo (google-veo) - "Supports extend, audio"
  - Sora (openai-sora) - "Up to 12 seconds"
- Duration: 4s, 6s, 8s, 12s
- Aspect ratio selector
- Audio toggle (Veo 3 only)
- Image-to-video option

### Step 6.4: Create MediaPreview

**File:** `frontend/src/lib/components/deck/studio/MediaPreview.svelte`

Requirements:
- Image: Full display with zoom
- Video: Player with controls
- Loading: Skeleton with progress
- Error: Message with retry
- Actions: Download, Edit, Save, Delete

### Step 6.5: Create AssetLibrary

**File:** `frontend/src/lib/components/deck/studio/AssetLibrary.svelte`

Requirements:
- Filter tabs: All, Images, Videos
- Search input
- Grid of thumbnails
- Click to preview
- Multi-select for bulk delete

### Step 6.6: Create GenerationHistory

**File:** `frontend/src/lib/components/deck/studio/GenerationHistory.svelte`

Requirements:
- Horizontal scroll carousel
- Click to load into preview
- Quick re-generate button
- Delete on hover
- Prompt preview tooltip

### Step 6.7: Create Studio Index

**File:** `frontend/src/lib/components/deck/studio/index.ts`

### Verification Checkpoint 6

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 7: Backend APIs

### Step 7.1: Create Agents API

**File:** `app/api/agents.py`

Endpoints:
- `POST /api/v1/agents/launch` - Launch background agent
- `GET /api/v1/agents` - List all agents
- `GET /api/v1/agents/{agent_id}` - Get agent details
- `GET /api/v1/agents/{agent_id}/logs` - Get logs and tasks
- `POST /api/v1/agents/{agent_id}/pause` - Pause agent
- `POST /api/v1/agents/{agent_id}/resume` - Resume agent
- `POST /api/v1/agents/{agent_id}/cancel` - Cancel agent
- `DELETE /api/v1/agents/{agent_id}` - Delete record
- `WebSocket /api/v1/agents/ws` - Real-time updates

Use in-memory storage for MVP. Simulated agent execution with progress updates.

### Step 7.2: Create Studio API

**File:** `app/api/studio.py`

Endpoints:
- `POST /api/v1/studio/image/generate` - Generate image
- `POST /api/v1/studio/video/generate` - Generate video
- `GET /api/v1/studio/generations` - List generations
- `GET /api/v1/studio/generations/{gen_id}` - Get status
- `DELETE /api/v1/studio/generations/{gen_id}` - Delete
- `GET /api/v1/studio/assets` - List saved assets
- `POST /api/v1/studio/assets` - Save to library
- `DELETE /api/v1/studio/assets/{asset_id}` - Delete asset
- `GET /api/v1/studio/providers` - List AI providers

Integrate with AI tools at `/opt/ai-tools/`.

### Step 7.3: Register Routes

**File:** `app/main.py`

Add:
```python
from app.api import agents, studio
app.include_router(agents.router)
app.include_router(studio.router)
```

### Step 7.4: Add Database Tables (Optional)

**File:** `app/db/database.py`

Add tables for persistence:
- `background_agents`
- `studio_generations`
- `studio_assets`

### Verification Checkpoint 7

```bash
cd /workspace/ai-hub && python -c "from app.main import app; print('OK')"
```

---

## Phase 8: Main Page Integration

### Step 8.1: Backup Original Page

```bash
cp frontend/src/routes/+page.svelte frontend/src/routes/+page.svelte.backup
```

### Step 8.2: Rewrite Main Page

**File:** `frontend/src/routes/+page.svelte`

Target: ~500-800 lines (down from 4600+)

Structure:
```svelte
<script lang="ts">
  import { DeckLayout, ActivityRail, ContextPanel, Dock } from '$lib/components/deck';
  import { Workspace, MobileWorkspace, ChatCard, AgentCard, CanvasCard, TerminalCard } from '$lib/components/deck/cards';
  import { AgentsView } from '$lib/components/deck/agents';
  import { StudioView } from '$lib/components/deck/studio';
  import { deck, agents, studio } from '$lib/stores';
  import { isAuthenticated } from '$lib/stores/auth';
  import { allTabs } from '$lib/stores/tabs';

  // Detect mobile on mount and resize
  $effect(() => {
    const checkMobile = () => deck.setIsMobile(window.innerWidth < 640);
    checkMobile();
    window.addEventListener('resize', checkMobile);
    return () => window.removeEventListener('resize', checkMobile);
  });

  // Convert tabs to cards on mount
  $effect(() => {
    // Sync existing tabs to deck cards
  });

  // Keyboard shortcuts
  onMount(() => {
    // Register: Cmd+K, Cmd+N, Escape, etc.
  });
</script>

{#if !$isAuthenticated}
  <!-- Redirect to login -->
{:else}
  <DeckLayout
    activeMode={$deck.activeMode}
    badges={computedBadges}
    contextCollapsed={$deck.contextPanelCollapsed}
    isMobile={$deck.isMobile}
    onModeChange={deck.setMode}
    ...
  >
    <svelte:fragment slot="workspace">
      {#if $deck.activeMode === 'workspace'}
        {#if $deck.isMobile}
          <!-- Mobile: Full-screen stacked cards -->
          <MobileWorkspace
            cards={$deck.cards}
            activeCardIndex={$deck.mobileActiveCardIndex}
            onCardChange={deck.setMobileActiveCardIndex}
            onCloseCard={deck.removeCard}
            onCreateCard={deck.addCard}
          />
        {:else}
          <!-- Desktop: Free-form draggable cards -->
          <Workspace
            cards={$deck.visibleCards}
            onCardFocus={deck.focusCard}
            onCardMove={deck.moveCard}
            onCardResize={deck.resizeCard}
            onCardSnap={deck.snapCard}
            onCreateCard={deck.addCard}
          >
            {#each $deck.visibleCards as card (card.id)}
              {#if card.type === 'chat'}
                <ChatCard {card} tabId={card.data.tabId} ... />
              {:else if card.type === 'agent'}
                <AgentCard {card} ... />
              {:else if card.type === 'canvas'}
                <CanvasCard {card} ... />
              {:else if card.type === 'terminal'}
                <TerminalCard {card} ... />
              {/if}
            {/each}
          </Workspace>
        {/if}
      {:else if $deck.activeMode === 'agents'}
        <AgentsView />
      {:else if $deck.activeMode === 'studio'}
        <StudioView />
      {:else}
        <FilesView /> <!-- placeholder -->
      {/if}
    </svelte:fragment>

    <svelte:fragment slot="context">
      <ContextPanel ... />
    </svelte:fragment>

    <!-- Dock only on desktop -->
    {#if !$deck.isMobile}
      <svelte:fragment slot="dock">
        <Dock
          minimizedCards={$deck.minimizedCards}
          runningAgents={$agents.running}
          activeGenerations={$studio.activeGenerations}
          onRestoreCard={deck.restoreCard}
        />
      </svelte:fragment>
    {/if}
  </DeckLayout>

  <!-- Modals (unchanged from current implementation) -->
  {#if showSettingsModal}<SettingsModal ... />{/if}
  {#if showSpotlight}<SpotlightSearch ... />{/if}
  {#if showProfileModal}<ProfileModal ... />{/if}
  <!-- etc -->
{/if}
```

### Step 8.3: Connect to Existing Stores

- Use `tabs` store for chat sessions and WebSocket
- Use `sessions` store for session list
- Use `profiles` and `projects` stores
- Use new `deck`, `agents`, `studio` stores

### Step 8.4: Preserve Keyboard Shortcuts

- Cmd+K → Spotlight
- Cmd+N → New chat card
- Escape → Close modals / deselect card
- Cmd+W → Close focused card
- Cmd+M → Minimize focused card

### Verification Checkpoint 8

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 9: Testing & Polish

### Step 9.1: Fix Any TypeScript Errors

Common issues:
- Missing imports
- Type mismatches
- Undefined properties
- Callback signature mismatches

### Step 9.2: Test Card Behaviors

Checklist:
- [ ] Cards can be dragged freely
- [ ] Cards snap to edges when dragged close
- [ ] Cards snap to half-screen when dragged to sides
- [ ] Cards maximize when double-clicking title bar
- [ ] Cards minimize to dock
- [ ] Click dock item restores card
- [ ] Cards resize from all 8 handles
- [ ] Cards respect minimum size
- [ ] Z-order updates on click (focused card on top)
- [ ] Cascade arranges cards diagonally
- [ ] Tile arranges cards in grid

### Step 9.3: Test ChatCard Feature Parity

**CRITICAL CHECKLIST - All must pass:**

- [ ] Context usage indicator shows correct percentage and colors
- [ ] Context tooltip shows all token breakdowns
- [ ] Profile selector works (groups, selection, create/manage)
- [ ] Project selector works (groups, selection, locked when session exists)
- [ ] Messages render correctly (user, assistant, tool, system)
- [ ] Markdown renders with syntax highlighting
- [ ] Code blocks have copy button
- [ ] Images display with download/copy buttons
- [ ] Videos play with controls
- [ ] File download cards work
- [ ] Copy button on assistant messages works
- [ ] Fork button works
- [ ] Streaming shows bouncing dots
- [ ] Streaming cursor blinks at end of text
- [ ] Tool use collapses/expands
- [ ] Tool status icons correct (spinner/check/error)
- [ ] Auto-scroll works
- [ ] Can scroll up to read history
- [ ] Model override pill works (admin)
- [ ] Mode override pill works (admin)
- [ ] Reset button works
- [ ] Attach button works (disabled when no project)
- [ ] File chips display and can be removed
- [ ] Command autocomplete (/) works
- [ ] File autocomplete (@) works
- [ ] Voice recording works
- [ ] Send button works
- [ ] Stop button works during streaming
- [ ] Permission banners appear and Allow/Deny work
- [ ] Question banners appear and can be answered
- [ ] WebSocket reconnection works

### Step 9.4: Visual Verification (Desktop)

- [ ] Activity Rail renders with all icons
- [ ] Mode switching works
- [ ] Context Panel collapses/expands
- [ ] Dock shows minimized cards and running processes
- [ ] Workspace mode: cards render and interact
- [ ] Agent mode: view renders
- [ ] Studio mode: view renders
- [ ] Empty workspace shows welcome state with create buttons
- [ ] Right-click workspace shows context menu

### Step 9.5: Mobile Verification

- [ ] Rail moves to bottom (horizontal)
- [ ] Dock is hidden
- [ ] Cards display full-screen
- [ ] Swipe left/right navigates between cards
- [ ] Dot indicators show and are tappable
- [ ] Empty state shows create buttons
- [ ] Card close button works
- [ ] Context panel becomes slide-up sheet
- [ ] Spotlight opens on mobile (via button or gesture)
- [ ] ChatCard works fully on mobile (all features)

### Step 9.6: Build & Deploy

```bash
# Build frontend
cd frontend && npm run build

# Copy to running app (if in Docker)
rm -rf /app/app/static/* && cp -r build/* /app/app/static/
```

---

## File Checklist

### Deck Components (10 files)

```
frontend/src/lib/components/deck/
├── types.ts
├── index.ts
├── DeckLayout.svelte
├── ActivityRail.svelte
├── ContextPanel.svelte
└── Dock.svelte
```

### Card Components (9 files)

```
frontend/src/lib/components/deck/cards/
├── types.ts
├── index.ts
├── BaseCard.svelte
├── Workspace.svelte
├── MobileWorkspace.svelte
├── ChatCard.svelte
├── AgentCard.svelte
├── CanvasCard.svelte
└── TerminalCard.svelte
```

### Chat Components (14 files) - EXTRACTED FROM +page.svelte

```
frontend/src/lib/components/chat/
├── index.ts
├── ChatHeader.svelte
├── MessageArea.svelte
├── UserMessage.svelte
├── AssistantMessage.svelte
├── ToolUseMessage.svelte
├── ToolResultMessage.svelte
├── StreamingIndicator.svelte
├── SystemMessage.svelte
├── SubagentMessage.svelte
├── ChatInput.svelte
├── CommandAutocomplete.svelte
├── FileAutocomplete.svelte
├── PermissionBanner.svelte
└── QuestionBanner.svelte
```

### Agent Components (8 files)

```
frontend/src/lib/components/deck/agents/
├── index.ts
├── AgentsView.svelte
├── AgentLauncher.svelte
├── AgentListItem.svelte
├── AgentDetails.svelte
├── AgentLogs.svelte
├── TaskTree.svelte
└── AgentStats.svelte
```

### Studio Components (7 files)

```
frontend/src/lib/components/deck/studio/
├── index.ts
├── StudioView.svelte
├── ImageGenerator.svelte
├── VideoGenerator.svelte
├── MediaPreview.svelte
├── AssetLibrary.svelte
└── GenerationHistory.svelte
```

### Stores (4 files)

```
frontend/src/lib/stores/
├── deck.ts
├── agents.ts
├── studio.ts
└── index.ts (update)
```

### Backend (2 files)

```
app/api/
├── agents.py
└── studio.py
```

### Modified Files

```
app/main.py (add router imports)
app/db/database.py (optional: add tables)
frontend/src/routes/+page.svelte (rewrite using extracted components)
```

---

## Tips for Success

1. **Phase 4 is critical** - Extract chat components FIRST before building ChatCard
2. **Build after each phase** - Don't wait until the end
3. **Fix errors immediately** - Don't accumulate technical debt
4. **Use Svelte 5 syntax** - `$props()`, `$state()`, `$derived()`, `$effect()`
5. **Export everything** - Every component needs an index.ts entry
6. **Type everything** - No `any` types unless absolutely necessary
7. **Test incrementally** - Verify each component works before moving on
8. **Keep the backup** - The original +page.svelte.backup is your safety net
9. **ChatCard must be exact** - If something works in +page.svelte, it must work in ChatCard

---

## Estimated Effort

| Phase | Components | Estimated Time |
|-------|------------|----------------|
| Phase 1: Core Layout | 6 files | 2-3 hours |
| Phase 2: Card System | 8 files | 4-5 hours |
| Phase 3: State Management | 4 files | 2-3 hours |
| Phase 4: Extract Chat | 14 files | 4-6 hours |
| Phase 5: Agent Mode | 8 files | 2-3 hours |
| Phase 6: Studio Mode | 7 files | 2-3 hours |
| Phase 7: Backend APIs | 2 files | 1-2 hours |
| Phase 8: Main Page | 1 file | 2-3 hours |
| Phase 9: Testing & Polish | - | 3-4 hours |

**Total: ~22-32 hours of focused work**

---

## Commands Reference

```bash
# Check frontend builds
cd frontend && npm run build

# Check TypeScript
cd frontend && npm run check

# Check backend imports
cd /workspace/ai-hub && python -c "from app.main import app; print('OK')"

# Deploy to running app (Docker)
cd frontend && npm run build && rm -rf /app/app/static/* && cp -r build/* /app/app/static/
```
