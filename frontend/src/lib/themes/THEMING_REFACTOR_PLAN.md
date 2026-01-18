# Theming Refactor Plan

> **Goal**: Audit and fix ALL components to follow the theming template rules.
> **Reference**: See `THEMING_TEMPLATE.md` for the authoritative guidelines.

---

## Phase 1: Core CSS Variables Update

### Task 1.1: Update app.css with new muted variants

Add the expanded muted variants and ensure all derived variables are properly defined:

**File**: `frontend/src/app.css`

**Add these variables** to both `.dark` and `.light` selectors:

```css
/* Muted variants */
--muted-light: /* lighter muted */;
--muted-dark: /* darker muted */;
--muted-border: /* border color derived from muted */;

/* Input states */
--input-bg: var(--muted);
--input-bg-hover: /* slightly lighter/darker */;
--input-bg-focus: var(--background);
--input-border-hover: /* intensified border */;
--input-placeholder: var(--muted-foreground);

/* Button gradient */
--button-primary-gradient: linear-gradient(135deg, var(--primary) 0%, color-mix(in oklch, var(--primary) 90%, oklch(0.3 0.05 250)) 100%);

/* Ensure accent is properly derived or overridable */
```

---

## Phase 2: Card Design System CSS Update

### Task 2.1: Update card-design-system.css

**File**: `frontend/src/lib/components/deck/cards/card-design-system.css`

**Changes needed**:
1. Ensure hover states use accent glow borders, not background fills
2. Verify all `.card-list-item:hover` uses the new pattern
3. Check all button variants follow the template

**Specific fixes**:
- Line ~283-291: Update `card-list-item:hover` to use accent glow border pattern
- Verify `--surface-hover` uses accent properly

---

## Phase 3: Component Audit & Fix

### 3.1 High-Priority Components (Most Used)

#### ChatInput.svelte
**File**: `frontend/src/lib/components/chat/ChatInput.svelte`
**Status**: Needs audit

**Check for**:
- Hover states using accent glow borders
- Focus states with ring
- All colors using variables
- Button styling following template

---

#### BaseCard.svelte
**File**: `frontend/src/lib/components/deck/cards/BaseCard.svelte`
**Status**: Reference implementation - verify it follows all rules

---

#### SettingsCard.svelte
**File**: `frontend/src/lib/components/deck/cards/SettingsCard.svelte`
**Status**: Large file, needs thorough audit

**Check for**:
- Tab styling uses `--primary` for active, `--muted-foreground` for inactive
- Form inputs use proper input variables
- Toggle switches use proper states
- Buttons follow hierarchy (primary gradient, secondary flat)

---

### 3.2 List-Based Components

#### ProfileCard.svelte
**File**: `frontend/src/lib/components/deck/cards/ProfileCard.svelte`
**Check**: List items, badges, hover states

---

#### ProjectCard.svelte
**File**: `frontend/src/lib/components/deck/cards/ProjectCard.svelte`
**Check**: List items, badges, hover states

---

#### SubagentCard.svelte
**File**: `frontend/src/lib/components/deck/cards/SubagentCard.svelte`
**Check**: Model badges, list items

---

#### HooksCard.svelte
**File**: `frontend/src/lib/components/deck/cards/HooksCard.svelte`
**Check**: Hook type badges, list items

---

#### PluginManagerCard.svelte
**File**: `frontend/src/lib/components/deck/cards/PluginManagerCard.svelte`
**Check**: Plugin list items, status badges

---

### 3.3 Content/Display Components

#### NotesCard.svelte
**File**: `frontend/src/lib/components/deck/cards/NotesCard.svelte`
**Check**: Note list items, editor styling

---

#### StatsCard.svelte
**File**: `frontend/src/lib/components/deck/cards/StatsCard.svelte`
**Check**: Chart colors using `--chart-*`, stat value styling

---

#### ChatCard.svelte
**File**: `frontend/src/lib/components/deck/cards/ChatCard.svelte`
**Check**: Message bubbles, streaming indicators

---

### 3.4 Interactive Components (Non-Card)

#### PermissionRequest.svelte
**File**: `frontend/src/lib/components/PermissionRequest.svelte`
**Status**: Has many inline color uses

**Specific fixes needed** (based on grep results):
- Line 552-558: Check accent/primary usage for borders
- Line 604-614: Check muted usage
- Line 627-686: Button styling audit

---

#### UserQuestion.svelte
**File**: `frontend/src/lib/components/UserQuestion.svelte`
**Status**: Similar to PermissionRequest

**Specific fixes needed**:
- Line 280-282: Step indicators using `--primary`
- Line 611-617: Accent/primary border usage
- Line 670-705: Button and input focus states

---

#### GroupDropdown.svelte
**File**: `frontend/src/lib/components/GroupDropdown.svelte`
**Check**: Dropdown item hover, selected states

---

### 3.5 Specialized Components

#### autocomplete-design-system.css
**File**: `frontend/src/lib/components/autocomplete/autocomplete-design-system.css`
**Status**: Needs audit for hover patterns

**Specific areas**:
- Line 176-183: `--surface-hover` and `--surface-active` usage
- Line 279-281: Badge styling

---

#### DeckLayout.svelte
**File**: `frontend/src/lib/components/deck/DeckLayout.svelte`
**Check**: Button styling, primary usage

---

#### RecentSessionsTimeline.svelte
**File**: `frontend/src/lib/components/deck/RecentSessionsTimeline.svelte`
**Check**: Timeline indicators, hover states, badges

---

#### PluginListItem.svelte
**File**: `frontend/src/lib/components/plugins/PluginListItem.svelte`
**Check**: Plugin state badges, toggle styling

---

#### PluginHookItem.svelte
**File**: `frontend/src/lib/components/hooks/PluginHookItem.svelte`
**Check**: Hook event type colors

---

#### SummaryCards.svelte (Analytics)
**File**: `frontend/src/lib/components/analytics/SummaryCards.svelte`
**Check**: Stats styling, borders

---

## Phase 4: Global Patterns Check

### 4.1 Grep for Anti-Patterns

Search for and fix these patterns across all files:

```bash
# Hardcoded colors
grep -r "background: #" frontend/src
grep -r "color: #" frontend/src
grep -r "border.*#" frontend/src
grep -r "oklch\([^v]" frontend/src  # oklch not from var()

# Background fills on hover (should be glow borders)
grep -r "hover.*background.*accent" frontend/src
grep -r ":hover.*background: var(--accent)" frontend/src

# Missing focus rings
grep -r ":focus\s*{" frontend/src  # Should be :focus-visible with ring
```

### 4.2 Ensure All Components Import Design System

Every card component should have:
```svelte
<script>
  import './card-design-system.css';
</script>
```

---

## Phase 5: Testing & Validation

### 5.1 Theme Preset Testing

After all fixes, test with multiple theme presets:
- Default dark theme
- Default light theme
- At least 2-3 custom presets with different primary colors

### 5.2 Visual Regression Checklist

- [ ] All buttons follow primary gradient / secondary flat pattern
- [ ] All hover states show accent glow borders
- [ ] All focus states show visible rings
- [ ] All badges use opacity-mixed backgrounds
- [ ] No hardcoded colors visible
- [ ] Light mode works correctly
- [ ] Dark mode works correctly
- [ ] Glass effects work correctly

---

## File Checklist

| File | Phase | Status |
|------|-------|--------|
| app.css | 1 | ⬜ Pending |
| card-design-system.css | 2 | ⬜ Pending |
| ChatInput.svelte | 3.1 | ⬜ Pending |
| BaseCard.svelte | 3.1 | ⬜ Pending |
| SettingsCard.svelte | 3.1 | ⬜ Pending |
| ProfileCard.svelte | 3.2 | ⬜ Pending |
| ProjectCard.svelte | 3.2 | ⬜ Pending |
| SubagentCard.svelte | 3.2 | ⬜ Pending |
| HooksCard.svelte | 3.2 | ⬜ Pending |
| PluginManagerCard.svelte | 3.2 | ⬜ Pending |
| NotesCard.svelte | 3.3 | ⬜ Pending |
| StatsCard.svelte | 3.3 | ⬜ Pending |
| ChatCard.svelte | 3.3 | ⬜ Pending |
| PermissionRequest.svelte | 3.4 | ⬜ Pending |
| UserQuestion.svelte | 3.4 | ⬜ Pending |
| GroupDropdown.svelte | 3.4 | ⬜ Pending |
| autocomplete-design-system.css | 3.5 | ⬜ Pending |
| DeckLayout.svelte | 3.5 | ⬜ Pending |
| RecentSessionsTimeline.svelte | 3.5 | ⬜ Pending |
| PluginListItem.svelte | 3.5 | ⬜ Pending |
| PluginHookItem.svelte | 3.5 | ⬜ Pending |
| SummaryCards.svelte | 3.5 | ⬜ Pending |
| Global anti-pattern fixes | 4 | ⬜ Pending |
| Testing | 5 | ⬜ Pending |

---

## Success Criteria

1. **Zero hardcoded colors** in component files
2. **All hover states** use accent glow border pattern
3. **All focus states** have visible `--ring` outline
4. **All buttons** follow primary gradient / secondary flat hierarchy
5. **All badges** use opacity-mixed backgrounds (15%) and borders (30%)
6. **All components** work correctly with different theme presets
7. **Light and dark modes** both work without issues
