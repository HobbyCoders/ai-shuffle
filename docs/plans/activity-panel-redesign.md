# Activity Panel Redesign Plan

## Overview

Transform the existing ContextPanel into a unified **Activity Panel** that consolidates all tracking and settings into one location. The panel will have:
1. **Fixed header** with active sessions (always visible)
2. **Tabbed content area** for different panel views (Threads, Settings, Agents, etc.)
3. **Context-aware overlays** that slide in for specific card settings (e.g., chat settings)

## Current State

### ContextPanel.svelte
- Located: `frontend/src/lib/components/deck/ContextPanel.svelte`
- Contains collapsible sections:
  - Active Threads
  - Recent Sessions (History)
  - Running Agents
  - Generations
  - Session Info
- Has collapse toggle button
- ~640 lines

### SettingsDrawer.svelte
- Located: `frontend/src/lib/components/chat/SettingsDrawer.svelte`
- Separate sliding drawer for chat settings
- Contains: Profile, Project, Model, Mode, Context Usage, Background Mode
- ~560 lines

## Proposed Architecture

```
┌─────────────────────────────────────┐
│  ACTIVITY PANEL                     │
├─────────────────────────────────────┤
│  ┌─────────────────────────────────┐│
│  │  ACTIVE SESSIONS (Fixed Header) ││
│  │  • Chat 1 (active)              ││
│  │  • Chat 2                       ││
│  │  • Agent: Build Feature         ││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│  [Threads] [Agents] [Studio] [Info] │  ← Tab Bar
├─────────────────────────────────────┤
│  ┌─────────────────────────────────┐│
│  │                                 ││
│  │  TAB CONTENT AREA               ││
│  │  (scrollable)                   ││
│  │                                 ││
│  └─────────────────────────────────┘│
├─────────────────────────────────────┤
│  ┌─────────────────────────────────┐│
│  │  OVERLAY (when active)          ││  ← Slides up from bottom
│  │  e.g., Chat Settings            ││     or slides in from right
│  │  - Profile, Project             ││
│  │  - Model, Mode                  ││
│  │  - Context usage                ││
│  └─────────────────────────────────┘│
└─────────────────────────────────────┘
```

## Component Structure

### 1. ActivityPanel.svelte (New - replaces ContextPanel)
```
frontend/src/lib/components/deck/ActivityPanel.svelte
```

**Props:**
```typescript
interface Props {
  collapsed?: boolean;
  activeTab?: 'threads' | 'agents' | 'studio' | 'info';

  // Active sessions (fixed header)
  activeSessions: ActiveSession[];

  // Tab content data
  recentSessions?: HistorySession[];
  runningAgents?: DeckAgent[];
  generations?: DeckGeneration[];
  sessionInfo?: SessionInfo;

  // Overlay state
  overlayType?: 'chat-settings' | 'agent-settings' | null;
  overlayData?: ChatSettingsData | AgentSettingsData;

  // Callbacks
  onToggleCollapse?: () => void;
  onTabChange?: (tab: string) => void;
  onSessionClick?: (session: ActiveSession) => void;
  onOverlayClose?: () => void;
  // ... other callbacks
}
```

### 2. ActivityHeader.svelte (New)
Fixed header showing all active sessions (chats + agents).

```typescript
interface ActiveSession {
  id: string;
  type: 'chat' | 'agent';
  title: string;
  status: 'active' | 'idle' | 'running' | 'error';
  isSelected: boolean;
  unread?: boolean;
}
```

### 3. ActivityTabs.svelte (New)
Tab bar component for switching between panel views.

**Tabs:**
- **Threads** - Recent sessions, history
- **Agents** - Running/completed background agents
- **Studio** - Active generations, gallery
- **Info** - Session stats, costs, tokens

### 4. ActivityOverlay.svelte (New)
Slide-in overlay for context-specific settings.

**Overlay Types:**
- `chat-settings` - Profile, Project, Model, Mode, Context Usage
- `agent-settings` - Agent configuration (future)
- `generation-settings` - Generation parameters (future)

### 5. Refactored Files

**ChatInput.svelte changes:**
- Remove SettingsDrawer import/usage
- Instead of opening drawer, emit event to open overlay
- `openSettings()` → `dispatch('openSettingsOverlay', { type: 'chat-settings', data: {...} })`

**+page.svelte changes:**
- Pass overlay state to ActivityPanel
- Handle overlay events from ChatInput
- Wire up overlay data flow

## Data Flow

```
ChatInput (gear icon click)
    ↓
dispatch('openSettingsOverlay', { type: 'chat-settings', data })
    ↓
+page.svelte (receives event)
    ↓
Sets overlayType + overlayData state
    ↓
ActivityPanel (receives overlay props)
    ↓
Renders ActivityOverlay with settings UI
    ↓
User makes changes
    ↓
Callbacks propagate back up to +page.svelte
    ↓
Updates tab store (profile, project, model, mode)
```

## Implementation Phases

### Phase 1: Create ActivityPanel Shell
1. Create `ActivityPanel.svelte` with basic structure
2. Create `ActivityHeader.svelte` for active sessions
3. Create `ActivityTabs.svelte` for tab navigation
4. Wire up in `DeckLayout.svelte` replacing ContextPanel

### Phase 2: Migrate Content
1. Move Threads content from ContextPanel
2. Move Agents content from ContextPanel
3. Move Generations content from ContextPanel
4. Move Session Info content from ContextPanel
5. Delete old ContextPanel.svelte

### Phase 3: Add Overlay System
1. Create `ActivityOverlay.svelte` base component
2. Create `ChatSettingsOverlay.svelte` (move content from SettingsDrawer)
3. Wire up overlay state in +page.svelte
4. Update ChatInput to use overlay instead of drawer
5. Delete old SettingsDrawer.svelte

### Phase 4: Polish & Integration
1. Add animations for overlay slide-in/out
2. Add keyboard navigation (Escape to close overlay)
3. Ensure mobile responsiveness
4. Test all interactions

## File Changes Summary

### New Files
- `frontend/src/lib/components/deck/ActivityPanel.svelte`
- `frontend/src/lib/components/deck/ActivityHeader.svelte`
- `frontend/src/lib/components/deck/ActivityTabs.svelte`
- `frontend/src/lib/components/deck/ActivityOverlay.svelte`
- `frontend/src/lib/components/deck/overlays/ChatSettingsOverlay.svelte`

### Modified Files
- `frontend/src/lib/components/deck/DeckLayout.svelte` - Use ActivityPanel
- `frontend/src/lib/components/deck/index.ts` - Export new components
- `frontend/src/routes/+page.svelte` - Wire up overlay state
- `frontend/src/lib/components/chat/ChatInput.svelte` - Use overlay events

### Deleted Files
- `frontend/src/lib/components/deck/ContextPanel.svelte`
- `frontend/src/lib/components/chat/SettingsDrawer.svelte`

## UI/UX Details

### Active Sessions Header
- Always visible at top of panel
- Compact horizontal list of active items
- Click to focus/switch to that card
- Shows status indicators (streaming, idle, error)
- Max 5-6 visible, scroll for more

### Tab Bar
- Icons + labels on desktop
- Icons only on narrow widths
- Active tab has underline/highlight
- Badge counts where relevant (e.g., 3 running agents)

### Overlay Behavior
- Slides up from bottom (covers ~60% of panel)
- Or slides in from right edge
- Has close button + responds to Escape
- Backdrop click closes (optional)
- Remembers scroll position

### Mobile Considerations
- Panel becomes bottom sheet on mobile
- Tabs become horizontal swipe
- Overlay becomes full-screen modal

## CSS Variables Needed
```css
--activity-panel-width: 320px;
--activity-header-height: 64px;
--activity-tabs-height: 44px;
--activity-overlay-height: 60%;
```

## Testing Checklist
- [ ] Panel collapse/expand works
- [ ] Tab switching works
- [ ] Active sessions clickable and focus correct card
- [ ] Overlay opens from ChatInput gear
- [ ] Overlay settings changes persist
- [ ] Overlay closes on Escape
- [ ] Mobile layout works
- [ ] No regressions in existing functionality
