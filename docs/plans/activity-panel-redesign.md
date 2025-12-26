# Activity Panel Redesign + Background Agent Consolidation

## Overview

Two major changes:

### Task 1: Activity Panel
Transform the existing ContextPanel into a unified **Activity Panel** that consolidates all tracking and settings into one location.

### Task 2: Remove Agent Cards
Remove standalone agent cards. Background agents are now launched directly from chat cards via the settings overlay. The settings panel needs full agent configuration (not just a toggle).

---

# TASK 1: Activity Panel Redesign

## Architecture

```
┌─────────────────────────────────────┐
│  ACTIVITY PANEL                     │
├─────────────────────────────────────┤
│  ┌─────────────────────────────────┐│
│  │  ACTIVE SESSIONS (Fixed Header) ││  ← Always visible
│  │  • Chat 1 (streaming...)        ││
│  │  • Chat 2                       ││
│  │  • Agent: Build Feature (42%)   ││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│  [Threads] [Agents] [Studio] [Info] │  ← Tab Bar
├─────────────────────────────────────┤
│  ┌─────────────────────────────────┐│
│  │                                 ││
│  │  TAB CONTENT AREA               ││  ← Changes per tab
│  │  (scrollable)                   ││
│  │                                 ││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│  ┌─────────────────────────────────┐│
│  │  OVERLAY (when active)          ││  ← Slides up for settings
│  │  e.g., Chat Settings            ││
│  └─────────────────────────────────┘│
└─────────────────────────────────────┘
```

## Current State

### ContextPanel.svelte
- Location: `frontend/src/lib/components/deck/ContextPanel.svelte`
- Collapsible sections: Active Threads, Recent Sessions, Running Agents, Generations, Session Info
- ~640 lines

### SettingsDrawer.svelte
- Location: `frontend/src/lib/components/chat/SettingsDrawer.svelte`
- Contains: Profile, Project, Model, Mode, Context Usage, Background Mode toggle
- ~560 lines

## New Components

### 1. ActivityPanel.svelte (replaces ContextPanel)
```typescript
interface Props {
  collapsed?: boolean;
  activeTab?: 'threads' | 'agents' | 'studio' | 'info';

  // Fixed header data
  activeSessions: ActiveSession[];

  // Tab content
  recentSessions?: HistorySession[];
  runningAgents?: BackgroundAgent[];
  generations?: DeckGeneration[];
  sessionInfo?: SessionInfo;

  // Overlay
  overlayType?: 'chat-settings' | null;
  overlayData?: ChatSettingsData;

  // Callbacks
  onToggleCollapse?: () => void;
  onTabChange?: (tab: string) => void;
  onSessionClick?: (session: ActiveSession) => void;
  onAgentClick?: (agent: BackgroundAgent) => void;
  onOverlayClose?: () => void;
}
```

### 2. ActivityHeader.svelte
Fixed header showing active chats + running background agents.

```typescript
interface ActiveSession {
  id: string;
  type: 'chat' | 'agent';
  title: string;
  status: 'active' | 'idle' | 'streaming' | 'running' | 'error';
  progress?: number;  // For agents
  isSelected: boolean;
  unread?: boolean;
}
```

### 3. ActivityTabs.svelte
Tab navigation bar.

| Tab | Content |
|-----|---------|
| Threads | Recent sessions, history |
| Agents | Background agents (running + completed) |
| Studio | Image/video generations |
| Info | Session stats, costs, tokens |

### 4. ActivityOverlay.svelte
Slide-up overlay container for settings panels.

### 5. ChatSettingsOverlay.svelte
Chat-specific settings (moved from SettingsDrawer, enhanced for background mode).

---

# TASK 2: Remove Agent Cards + Enhance Background Settings

## Current Agent Card System (TO BE REMOVED)

### Files to Delete
- `frontend/src/lib/components/deck/cards/AgentCard.svelte`
- `frontend/src/lib/components/deck/agents/AgentsView.svelte`
- Related agent card types/handlers in `+page.svelte`

### Files to Modify
- `frontend/src/lib/components/deck/cards/index.ts` - Remove AgentCard export
- `frontend/src/lib/components/deck/cards/types.ts` - Remove agent card type
- `frontend/src/routes/+page.svelte` - Remove agent card handling
- `frontend/src/lib/components/deck/CreateMenu.svelte` - Remove "Background Agent" option

## Enhanced Chat Settings for Background Mode

The settings overlay needs to support full background agent configuration, not just a toggle.

### Current SettingsDrawer Structure
```
- Profile selector
- Project selector
- Model selector
- Permission Mode selector
- Context Usage display
- Background Mode toggle (just on/off)
```

### New ChatSettingsOverlay Structure
```
┌─────────────────────────────────────┐
│  Chat Settings                   ✕  │
├─────────────────────────────────────┤
│  CONTEXT                            │
│  ┌─────────────────────────────────┐│
│  │ Profile    [Default ▾]         ││
│  │ Project    [ai-shuffle ▾]          ││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│  MODEL                              │
│  ┌─────────────────────────────────┐│
│  │ Model      [Sonnet ▾] From Prof││
│  │ Mode       [Ask ▾]    From Prof││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│  CONTEXT USAGE                      │
│  ┌─────────────────────────────────┐│
│  │ ████████████░░░░░░ 67%         ││
│  │ 134k / 200k tokens              ││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│  ┌─────────────────────────────────┐│
│  │ ☐ Run in Background             ││  ← Toggle
│  └─────────────────────────────────┘│
│                                     │
│  (When enabled, shows below:)       │
│  ┌─────────────────────────────────┐│
│  │ BACKGROUND AGENT CONFIG         ││
│  │                                 ││
│  │ Task Name                       ││
│  │ [Auto-generated from prompt   ] ││
│  │                                 ││
│  │ Branch                          ││
│  │ [feature/my-task ▾]  + New     ││
│  │                                 ││
│  │ ☐ Auto-create PR when done     ││
│  │ ☐ Auto-merge to main           ││
│  │                                 ││
│  │ Max Duration                    ││
│  │ [30 minutes ▾]                  ││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│         [Launch Agent]              │  ← Only when background mode on
└─────────────────────────────────────┘
```

### Background Agent Config Props
```typescript
interface BackgroundAgentConfig {
  taskName: string;           // Auto-generated from prompt, editable
  branch: string;             // Git branch to work on
  createNewBranch: boolean;   // Create new branch from main
  autoPR: boolean;            // Create PR when done
  autoMerge: boolean;         // Auto-merge PR (dangerous)
  maxDurationMinutes: number; // Timeout (default 30)
}
```

### Branch Selector Component
Need to fetch available branches from the project's git repo.

```typescript
// API call to get branches
GET /api/projects/{projectId}/branches
Response: { branches: string[], current: string, default: string }
```

If branch doesn't exist, offer to create from default branch.

---

## Implementation Phases

### Phase 1: Activity Panel Shell
1. Create `ActivityPanel.svelte` basic structure
2. Create `ActivityHeader.svelte` for active sessions
3. Create `ActivityTabs.svelte` for navigation
4. Wire into `DeckLayout.svelte` replacing ContextPanel

### Phase 2: Migrate ContextPanel Content
1. Move Threads tab content
2. Move Agents tab content
3. Move Studio tab content (generations)
4. Move Info tab content
5. Delete `ContextPanel.svelte`

### Phase 3: Create Overlay System
1. Create `ActivityOverlay.svelte` container
2. Create `ChatSettingsOverlay.svelte` with full background config
3. Add branch selector component
4. Wire overlay state in `+page.svelte`
5. Update ChatInput to trigger overlay

### Phase 4: Remove Agent Cards
1. Remove AgentCard.svelte
2. Remove AgentsView.svelte
3. Remove agent card type from CreateMenu
4. Clean up +page.svelte handlers
5. Update keyboard shortcuts

### Phase 5: Polish
1. Animations for overlay
2. Keyboard navigation
3. Mobile responsiveness
4. Testing

---

## File Changes Summary

### New Files
```
frontend/src/lib/components/deck/
├── ActivityPanel.svelte
├── ActivityHeader.svelte
├── ActivityTabs.svelte
├── ActivityOverlay.svelte
└── overlays/
    └── ChatSettingsOverlay.svelte

frontend/src/lib/components/common/
└── BranchSelector.svelte
```

### Modified Files
```
frontend/src/lib/components/deck/DeckLayout.svelte
frontend/src/lib/components/deck/index.ts
frontend/src/lib/components/deck/CreateMenu.svelte
frontend/src/lib/components/deck/cards/index.ts
frontend/src/lib/components/deck/cards/types.ts
frontend/src/lib/components/chat/ChatInput.svelte
frontend/src/routes/+page.svelte
```

### Deleted Files
```
frontend/src/lib/components/deck/ContextPanel.svelte
frontend/src/lib/components/deck/cards/AgentCard.svelte
frontend/src/lib/components/deck/agents/AgentsView.svelte
frontend/src/lib/components/chat/SettingsDrawer.svelte
```

---

## API Changes Needed

### New Endpoint: Get Project Branches
```
GET /api/projects/{projectId}/branches

Response:
{
  "branches": ["main", "develop", "feature/auth", "feature/ui-redesign"],
  "current": "main",
  "default": "main"
}
```

### Existing Agent Launch (modify if needed)
```
POST /api/agents/launch

Body:
{
  "name": "Build auth feature",
  "prompt": "...",
  "profileId": "...",
  "projectId": "...",
  "branch": "feature/auth",        // NEW: specific branch
  "createBranch": true,            // NEW: create if doesn't exist
  "autoPR": true,
  "autoMerge": false,
  "maxDurationMinutes": 30
}
```

---

## Testing Checklist

### Activity Panel
- [ ] Panel collapse/expand
- [ ] Tab switching
- [ ] Active sessions header shows chats + agents
- [ ] Click session focuses correct card
- [ ] Agent progress shown in header

### Settings Overlay
- [ ] Opens from ChatInput gear icon
- [ ] Profile/Project/Model/Mode work
- [ ] Background mode toggle reveals config
- [ ] Branch selector loads branches
- [ ] Can create new branch
- [ ] Launch Agent button works
- [ ] Closes on Escape / backdrop click

### Agent Cards Removed
- [ ] No agent card option in CreateMenu
- [ ] Keyboard shortcut removed (Cmd+Shift+B → repurpose?)
- [ ] Existing agent references cleaned up
- [ ] No console errors

### Background Agents
- [ ] Launch from chat settings works
- [ ] Agent appears in Active Sessions header
- [ ] Agent appears in Agents tab
- [ ] Agent progress updates
- [ ] Can click agent to view logs/output
