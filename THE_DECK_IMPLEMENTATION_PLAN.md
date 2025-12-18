# The Deck - Complete Implementation Plan

## Overview

Transform AI Hub from a chat-only interface into a **full AI workspace** with three distinct modes. This plan provides step-by-step instructions for implementing "The Deck" architecture.

---

## Vision

Instead of tabs and modals, "The Deck" is a fluid workspace where activities coexist and users shift *focus* rather than *location*.

### Layout Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ [64px]           [fluid]                         [320px]        │
│ ┌─────┐ ┌─────────────────────────────────┐ ┌─────────────────┐ │
│ │     │ │                                 │ │ Active Threads  │ │
│ │ 💬  │ │                                 │ │ ├─ Session 1    │ │
│ │ 🤖  │ │      CARD DECK AREA             │ │ └─ Session 2    │ │
│ │ 🎨  │ │                                 │ │                 │ │
│ │ 📁  │ │   (stack/split/focus/grid)      │ │ Running Agents  │ │
│ │     │ │                                 │ │ ├─ Agent 1 🟢   │ │
│ │     │ │                                 │ │ └─ Agent 2 🟢   │ │
│ │     │ │                                 │ │                 │ │
│ │ ⚙️  │ │                                 │ │ Generations     │ │
│ └─────┘ └─────────────────────────────────┘ │ └─ Image 🔄     │ │
│ RAIL                                        └─────────────────┘ │
│         ┌───────────────────────────────────────────────────┐   │
│         │ [🤖][🤖][🤖]  │  [🖼️][🎬]  │  [+Chat][+Agent][+Create] │
│         └───────────────────────────────────────────────────┘   │
│                              DOCK [56px]                        │
└─────────────────────────────────────────────────────────────────┘
```

### Three Modes

1. **Chat Mode** - Card-based conversations with WebSocket streaming
2. **Agent Mode** - Launch and monitor autonomous background agents
3. **Studio Mode** - Direct image/video creation without chat

---

## Phase 1: Core Layout Foundation

### Step 1.1: Create Types

**File:** `frontend/src/lib/components/deck/types.ts`

```typescript
export type ActivityMode = 'chat' | 'agents' | 'studio' | 'files';

export interface ActivityBadges {
  chat?: { count?: number; dot?: boolean; variant?: 'default' | 'warning' | 'error' | 'success' };
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
```

### Step 1.2: Create DeckLayout Component

**File:** `frontend/src/lib/components/deck/DeckLayout.svelte`

Key requirements:
- CSS Grid layout with 4 areas: rail, main, context, dock
- Responsive: on mobile (<640px), rail moves to bottom
- Context panel collapsible (grid column goes to 0)
- Smooth transitions (300ms)

```css
.deck-layout {
  display: grid;
  grid-template-columns: 64px 1fr 320px;
  grid-template-rows: 1fr 56px;
  grid-template-areas:
    "rail main context"
    "rail dock dock";
  height: 100vh;
}

.deck-layout.context-collapsed {
  grid-template-columns: 64px 1fr 0;
}

/* Mobile */
@media (max-width: 640px) {
  .deck-layout {
    grid-template-columns: 1fr;
    grid-template-rows: 1fr 56px 64px;
    grid-template-areas:
      "main"
      "dock"
      "rail";
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
- 4 activity buttons: Chat, Agents, Studio, Files
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
  1. Active Threads - list of sessions
  2. Running Agents - agents with status dots
  3. Generations - media with thumbnails
  4. Session Info - tokens, cost, model
- Glassmorphism background (`bg-card/80 backdrop-blur-xl`)

### Step 1.5: Create Dock Component

**File:** `frontend/src/lib/components/deck/Dock.svelte`

Requirements:
- Left section: Agent avatars with circular progress rings (SVG)
- Center section: Generation thumbnails with spinners
- Right section: Quick action buttons (+Chat, +Agent, +Create)
- Tooltips on all interactive elements

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

## Phase 2: Card System

### Step 2.1: Create Card Types

**File:** `frontend/src/lib/components/deck/cards/types.ts`

```typescript
export type CardType = 'chat' | 'agent' | 'canvas' | 'terminal';
export type LayoutMode = 'stack' | 'split' | 'focus' | 'grid';

export interface DeckCard {
  id: string;
  type: CardType;
  title: string;
  pinned: boolean;
  minimized: boolean;
  data?: any;
  createdAt?: Date;
  updatedAt?: Date;
}
```

### Step 2.2: Create BaseCard Component

**File:** `frontend/src/lib/components/deck/cards/BaseCard.svelte`

Requirements:
- Header: title, type icon, pin button, minimize button, close button
- Content area (slot/snippet)
- Footer area (optional slot/snippet)
- States: active (glow border), inactive (dimmed), minimized
- Glassmorphism: `bg-card/80 backdrop-blur-xl`
- Rounded corners, subtle shadow

### Step 2.3: Create CardContainer Component

**File:** `frontend/src/lib/components/deck/cards/CardContainer.svelte`

Requirements:
- 4 layout modes:
  - **stack**: Cards offset vertically (8px), peek visibility
  - **split**: 2-3 cards side by side with gap
  - **focus**: Single card expanded
  - **grid**: Thumbnail overview
- Layout switcher UI (4 small icons)
- Keyboard navigation (arrow keys)
- Click to activate card

### Step 2.4: Create ChatCard Component

**File:** `frontend/src/lib/components/deck/cards/ChatCard.svelte`

This is the most complex card. Create sub-components:

**Files in `frontend/src/lib/components/deck/cards/chat/`:**

1. **MessageList.svelte** - Scrollable container, auto-scroll on new messages
2. **UserMessage.svelte** - Right-aligned bubble, primary color
3. **AssistantMessage.svelte** - Left-aligned, markdown rendering with `marked`
4. **ToolUseMessage.svelte** - Collapsible details/summary with status icons
5. **StreamingIndicator.svelte** - Animated bouncing dots
6. **ChatInput.svelte** - Auto-grow textarea, send button, stop button during streaming
7. **index.ts** - Export all

ChatCard requirements:
- Connect to existing `tabs` store for WebSocket streaming
- Props: `tabId` to identify which tab's data to display
- Show messages from `$allTabs.find(t => t.id === tabId).messages`
- Handle `sendMessage` and `stopStreaming` via store

### Step 2.5: Create AgentCard Component

**File:** `frontend/src/lib/components/deck/cards/AgentCard.svelte`

Requirements:
- Status header with duration
- Branch badge
- Mini task tree
- Last few log lines
- Control buttons: Pause, Resume, Intervene, Cancel

### Step 2.6: Create CanvasCard Component

**File:** `frontend/src/lib/components/deck/cards/CanvasCard.svelte`

Requirements:
- Large preview area
- Prompt input
- Provider selector
- Generate button
- Variations thumbnails

### Step 2.7: Create TerminalCard Component

**File:** `frontend/src/lib/components/deck/cards/TerminalCard.svelte`

Requirements:
- xterm.js embed (dynamic import for SSR safety)
- Clear and copy buttons
- Connection status

### Step 2.8: Create Card Index

**File:** `frontend/src/lib/components/deck/cards/index.ts`

Export all card components and types.

### Verification Checkpoint 2

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 3: State Management

### Step 3.1: Create Deck Store

**File:** `frontend/src/lib/stores/deck.ts`

```typescript
interface DeckState {
  activeMode: ActivityMode;
  cards: DeckCard[];
  activeCardId: string | null;
  cardLayout: LayoutMode;
  contextPanelCollapsed: boolean;
  runningAgents: DeckAgent[];
  activeGenerations: DeckGeneration[];
}
```

Actions:
- `setMode(mode)`
- `addCard(card)`, `removeCard(id)`, `activateCard(id)`
- `minimizeCard(id)`, `pinCard(id)`
- `setCardLayout(layout)`
- `toggleContextPanel()`
- Agent and generation management

Derived stores:
- `activeCard`, `visibleCards`, `minimizedCards`
- `chatCards`, `agentCards`
- `activeMode`, `cardLayout`, `contextPanelCollapsed`

Persistence: localStorage with debounced saves (500ms)

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

## Phase 4: Agent Mode Components

### Step 4.1: Create AgentsView

**File:** `frontend/src/lib/components/deck/agents/AgentsView.svelte`

Requirements:
- Header with "Launch Agent" button
- Tabs: Running, Queued, Completed, Failed, Stats
- List of AgentListItem components
- Empty states for each tab

### Step 4.2: Create AgentLauncher

**File:** `frontend/src/lib/components/deck/agents/AgentLauncher.svelte`

Requirements:
- Modal/panel design
- Task name input
- Large prompt textarea
- Options: auto-branch, auto-PR, max duration
- Profile and project selectors
- Launch button

### Step 4.3: Create AgentListItem

**File:** `frontend/src/lib/components/deck/agents/AgentListItem.svelte`

Requirements:
- Status indicator (colored dot, animated for running)
- Agent name and status text
- Progress bar
- Duration badge
- Quick actions: Pause, View, Cancel

### Step 4.4: Create AgentDetails

**File:** `frontend/src/lib/components/deck/agents/AgentDetails.svelte`

Requirements:
- Full status header with metadata
- Branch info with GitHub link
- Tabs: Tasks (with TaskTree) and Logs
- Control buttons

### Step 4.5: Create AgentLogs

**File:** `frontend/src/lib/components/deck/agents/AgentLogs.svelte`

Requirements:
- Virtual scroll for performance
- Search within logs
- Log level filtering (all, info, warn, error)
- Timestamp toggle
- Auto-scroll toggle
- Copy all button

### Step 4.6: Create TaskTree

**File:** `frontend/src/lib/components/deck/agents/TaskTree.svelte`

Requirements:
- Recursive tree structure
- Status icons: ✓ (completed), ⟳ (running), ○ (pending), ✗ (failed)
- Expandable/collapsible branches
- Progress percentage
- CSS-based connector lines

### Step 4.7: Create AgentStats

**File:** `frontend/src/lib/components/deck/agents/AgentStats.svelte`

Requirements:
- Time filter (today/week/month/all)
- Stats: total agents, success rate, avg completion time
- Activity chart (7-day bar chart)

### Step 4.8: Create Agents Index

**File:** `frontend/src/lib/components/deck/agents/index.ts`

### Verification Checkpoint 4

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 5: Studio Mode Components

### Step 5.1: Create StudioView

**File:** `frontend/src/lib/components/deck/studio/StudioView.svelte`

Requirements:
- Split layout: 60% preview, 40% controls
- Tabs: Image, Video
- Shows active generation or empty state
- Asset library toggle

### Step 5.2: Create ImageGenerator

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

### Step 5.3: Create VideoGenerator

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

### Step 5.4: Create MediaPreview

**File:** `frontend/src/lib/components/deck/studio/MediaPreview.svelte`

Requirements:
- Image: Full display with zoom
- Video: Player with controls
- Loading: Skeleton with progress
- Error: Message with retry
- Actions: Download, Edit, Save, Delete

### Step 5.5: Create AssetLibrary

**File:** `frontend/src/lib/components/deck/studio/AssetLibrary.svelte`

Requirements:
- Filter tabs: All, Images, Videos
- Search input
- Grid of thumbnails
- Click to preview
- Multi-select for bulk delete

### Step 5.6: Create GenerationHistory

**File:** `frontend/src/lib/components/deck/studio/GenerationHistory.svelte`

Requirements:
- Horizontal scroll carousel
- Click to load into preview
- Quick re-generate button
- Delete on hover
- Prompt preview tooltip

### Step 5.7: Create Studio Index

**File:** `frontend/src/lib/components/deck/studio/index.ts`

### Verification Checkpoint 5

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 6: Backend APIs

### Step 6.1: Create Agents API

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

### Step 6.2: Create Studio API

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

### Step 6.3: Register Routes

**File:** `app/main.py`

Add:
```python
from app.api import agents, studio
app.include_router(agents.router)
app.include_router(studio.router)
```

### Step 6.4: Add Database Tables (Optional)

**File:** `app/db/database.py`

Add tables for persistence:
- `background_agents`
- `studio_generations`
- `studio_assets`

### Verification Checkpoint 6

```bash
cd /workspace/ai-hub && python -c "from app.main import app; print('OK')"
```

---

## Phase 7: Main Page Integration

### Step 7.1: Backup Original Page

```bash
cp frontend/src/routes/+page.svelte frontend/src/routes/+page.svelte.backup
```

### Step 7.2: Rewrite Main Page

**File:** `frontend/src/routes/+page.svelte`

Target: ~500-800 lines (down from 4600+)

Structure:
```svelte
<script lang="ts">
  // Imports: auth stores, tabs store, deck stores, deck components, modals
  // State: modal visibility flags, keyboard registrations
  // Derived: convert tabs to deck cards, sessions to DeckSession format
  // Lifecycle: onMount for auth check and store init
  // Handlers: mode change, card actions, dock actions
</script>

<!-- Auth check -->
{#if !$isAuthenticated}
  <!-- Redirect to login -->
{:else}
  <DeckLayout
    activeMode={...}
    badges={...}
    sessions={...}
    agents={...}
    generations={...}
    onModeChange={...}
    ...
  >
    <!-- Mode-specific content -->
    {#if $activeMode === 'chat'}
      <CardContainer cards={chatCards} ...>
        {#each chatCards as card}
          <ChatCard tabId={card.id} ... />
        {/each}
      </CardContainer>
    {:else if $activeMode === 'agents'}
      <AgentsView ... />
    {:else if $activeMode === 'studio'}
      <StudioView ... />
    {:else}
      <!-- Files placeholder -->
    {/if}
  </DeckLayout>

  <!-- Modals -->
  {#if showSettingsModal}<SettingsModal ... />{/if}
  {#if showSpotlight}<SpotlightSearch ... />{/if}
  <!-- etc -->
{/if}
```

### Step 7.3: Connect to Existing Stores

- Use `tabs` store for chat sessions and WebSocket
- Use `sessions` store for session list
- Use `profiles` and `projects` stores
- Use new `deck`, `agents`, `studio` stores

### Step 7.4: Preserve Keyboard Shortcuts

- Cmd+K → Spotlight
- Cmd+N → New chat
- Escape → Close modals

### Verification Checkpoint 7

```bash
cd frontend && npm run build && npm run check
```

---

## Phase 8: Testing & Polish

### Step 8.1: Fix Any TypeScript Errors

Common issues:
- Missing imports
- Type mismatches
- Undefined properties
- Callback signature mismatches

### Step 8.2: Create E2E Tests (Optional)

**File:** `frontend/playwright.config.ts`

**Files in `frontend/tests/`:**
- `deck-layout.spec.ts`
- `chat-mode.spec.ts`
- `agents-mode.spec.ts`
- `studio-mode.spec.ts`
- `navigation.spec.ts`
- `utils.ts`

### Step 8.3: Visual Verification

Checklist:
- [ ] Activity Rail renders with all icons
- [ ] Mode switching works
- [ ] Context Panel collapses/expands
- [ ] Dock shows running processes
- [ ] Chat mode: messages display, streaming works
- [ ] Agent mode: launcher opens, list renders
- [ ] Studio mode: generators render, providers selectable
- [ ] Mobile: rail moves to bottom

### Step 8.4: Build & Deploy

```bash
# Build frontend
cd frontend && npm run build

# Copy to running app (if in Docker)
rm -rf /app/app/static/* && cp -r build/* /app/app/static/
```

---

## File Checklist

### Components (36 files)

```
frontend/src/lib/components/deck/
├── types.ts
├── index.ts
├── DeckLayout.svelte
├── ActivityRail.svelte
├── ContextPanel.svelte
├── Dock.svelte
├── cards/
│   ├── types.ts
│   ├── index.ts
│   ├── BaseCard.svelte
│   ├── CardContainer.svelte
│   ├── ChatCard.svelte
│   ├── AgentCard.svelte
│   ├── CanvasCard.svelte
│   ├── TerminalCard.svelte
│   └── chat/
│       ├── index.ts
│       ├── MessageList.svelte
│       ├── UserMessage.svelte
│       ├── AssistantMessage.svelte
│       ├── ToolUseMessage.svelte
│       ├── StreamingIndicator.svelte
│       └── ChatInput.svelte
├── agents/
│   ├── index.ts
│   ├── AgentsView.svelte
│   ├── AgentLauncher.svelte
│   ├── AgentListItem.svelte
│   ├── AgentDetails.svelte
│   ├── AgentLogs.svelte
│   ├── TaskTree.svelte
│   └── AgentStats.svelte
└── studio/
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
frontend/src/routes/+page.svelte (rewrite)
```

---

## Tips for Success

1. **Build after each phase** - Don't wait until the end
2. **Fix errors immediately** - Don't accumulate technical debt
3. **Use Svelte 5 syntax** - `$props()`, `$state()`, `$derived()`, `$effect()`
4. **Export everything** - Every component needs an index.ts entry
5. **Type everything** - No `any` types unless absolutely necessary
6. **Test incrementally** - Verify each component works before moving on
7. **Keep the backup** - The original +page.svelte.backup is your safety net

---

## Estimated Effort

| Phase | Components | Estimated Time |
|-------|------------|----------------|
| Phase 1: Core Layout | 6 files | 2-3 hours |
| Phase 2: Card System | 12 files | 3-4 hours |
| Phase 3: State Management | 4 files | 2-3 hours |
| Phase 4: Agent Mode | 8 files | 2-3 hours |
| Phase 5: Studio Mode | 7 files | 2-3 hours |
| Phase 6: Backend APIs | 2 files | 1-2 hours |
| Phase 7: Main Page | 1 file | 2-3 hours |
| Phase 8: Testing & Polish | - | 2-3 hours |

**Total: ~16-24 hours of focused work**

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
