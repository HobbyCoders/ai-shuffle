# Card Design Polish Manifest

> This document defines the cohesive design language for all deck cards.
> Reference this when creating or modifying any card component.

---

## Design Philosophy: "Neo-Glass Brutalism"

A fusion of:
- **Data Brutalism**: Monospace numbers, stark contrasts, raw data presentation
- **Luxury Dashboard**: Glass morphism, subtle depth, refined micro-interactions
- **Technical Precision**: 8px grid, systematic spacing, deliberate typography

**Visual Signature:**
- Glass surfaces with subtle blur and transparency
- Primary color glows on interactive elements
- Layered shadows creating depth
- Monospace numbers for data emphasis
- Smooth but restrained animations

---

## Required Setup

Every card MUST import the design system:
```svelte
<script lang="ts">
  import './card-design-system.css';
  // ... rest of imports
</script>
```

Apply the `.card-system` class to enable token scoping:
```svelte
<div class="card-system my-card-content">
  <!-- content -->
</div>
```

---

## Typography Scale (Use These)

| Token | Size | Use Case |
|-------|------|----------|
| `--text-xs` | 11px | Micro labels, metadata, timestamps |
| `--text-sm` | 12px | Captions, badges, secondary text |
| `--text-base` | 13px | Body text, descriptions |
| `--text-md` | 14px | Emphasized text, item names |
| `--text-lg` | 16px | Section titles |
| `--text-xl` | 18px | Card titles (if custom header) |

**Font Families:**
- Body: `var(--card-font-display)` - Inter or system
- Code/Data: `var(--card-font-mono)` - JetBrains Mono or SF Mono

**Letter Spacing:**
- `--tracking-tight`: -0.01em (headings)
- `--tracking-normal`: 0 (body)
- `--tracking-wide`: 0.02em (small text)
- `--tracking-caps`: 0.06em (uppercase labels)

---

## Spacing System (8px Grid)

| Token | Value | Use Case |
|-------|-------|----------|
| `--space-1` | 4px | Tight gaps, icon margins |
| `--space-2` | 8px | Standard gap between elements |
| `--space-3` | 12px | Padding inside small elements |
| `--space-4` | 16px | Standard padding |
| `--space-5` | 20px | Generous padding |
| `--space-6` | 24px | Section spacing |
| `--space-8` | 32px | Large section breaks |

**Examples:**
```css
/* Good - uses tokens */
.my-item {
  padding: var(--space-3) var(--space-4);
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}

/* Bad - arbitrary values */
.my-item {
  padding: 10px 15px;
  gap: 6px;
  margin-bottom: 14px;
}
```

---

## Color System

### Surface Colors (for backgrounds)
- `--surface-0`: Card background (var(--card))
- `--surface-1`: Elevated/muted (60% muted, 40% transparent)
- `--surface-2`: Higher elevation (80% accent)
- `--surface-hover`: Hover state
- `--surface-active`: Active/pressed state

### Border Colors
- `--border-subtle`: Very faint (60% transparent)
- `--border-default`: Standard (var(--border))
- `--border-emphasis`: Highlighted (40% primary)
- `--border-glow`: For glow effects (50% primary transparent)

### Text Colors
- `--text-primary`: Main text (var(--foreground))
- `--text-secondary`: Muted text (var(--muted-foreground))
- `--text-tertiary`: Very muted (70% muted-foreground)
- `--text-accent`: Highlighted (var(--primary))

### Semantic Colors
- `var(--primary)` / `var(--primary-foreground)`: Actions, links
- `var(--success)`: Positive states, confirmations
- `var(--warning)`: Caution, pending
- `var(--destructive)`: Errors, delete actions
- `var(--info)`: Informational

---

## Shadow System

| Token | Use Case |
|-------|----------|
| `--shadow-inset` | Subtle inner glow on inputs |
| `--shadow-xs` | Small elements, badges |
| `--shadow-sm` | Cards, buttons |
| `--shadow-md` | Elevated cards, dropdowns |
| `--shadow-lg` | Modals, focused elements |
| `--shadow-glow-primary` | Active/focused glow |
| `--shadow-glow-soft` | Subtle accent glow |

---

## Border Radius

Inherits from `--radius` theme variable. Use multipliers:
- `--radius-sm`: 0.5x (small chips, badges)
- `--radius-md`: 0.833x (buttons, inputs)
- `--radius-lg`: 1.167x (cards, sections)
- `--radius-xl`: 1.5x (modals, large containers)
- `--radius-full`: 9999px (pills, avatars)

---

## Transitions

| Token | Duration | Use Case |
|-------|----------|----------|
| `--transition-fast` | 100ms | Micro-interactions |
| `--transition-base` | 150ms | Standard state changes |
| `--transition-smooth` | 200ms | Larger movements |
| `--transition-spring` | 300ms | Bouncy/playful |

All use `cubic-bezier(0.4, 0, 0.2, 1)` easing (except spring: 0.34, 1.56, 0.64, 1).

---

## Component Classes (Use These)

### Card Content Container
```css
.card-content {
  /* Already defined - scrollable content area */
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}
```

### Card Header (Custom Header Inside Content)
```css
.card-header {
  /* For custom headers inside BaseCard content */
  /* Already styled - use if you need a secondary header */
}
```

### Search Input
```html
<div class="card-search-container">
  <SearchIcon class="card-search-icon" />
  <input class="card-search-input" placeholder="Search..." />
</div>
```

### List Items
```html
<div class="card-list-item">
  <div class="card-item-icon"><Icon /></div>
  <div class="card-item-content">
    <div class="card-item-header">
      <span class="card-item-name">Item Name</span>
      <span class="card-badge card-badge--primary">Badge</span>
    </div>
    <p class="card-item-description">Description text...</p>
  </div>
  <div class="card-item-actions">
    <button class="card-action-btn"><EditIcon /></button>
  </div>
</div>
```

### Badges
Available badge variants:
- `.card-badge--primary`: Primary accent
- `.card-badge--muted`: Neutral
- `.card-badge--success`: Green
- `.card-badge--warning`: Yellow/amber
- `.card-badge--info`: Blue
- `.card-badge--haiku|sonnet|opus`: Model colors

### Buttons
```html
<button class="card-btn-primary">Primary Action</button>
<button class="card-btn-secondary">Secondary</button>
<button class="card-btn-destructive">Delete</button>
<button class="card-action-btn">Icon Only</button>
```

### Form Elements
```html
<div class="card-form-section">
  <label class="card-form-label">Field Name</label>
  <input class="card-form-input" />
  <span class="card-form-hint">Help text</span>
</div>

<textarea class="card-form-input card-form-textarea"></textarea>
```

### Tabs
```html
<div class="card-tabs">
  <button class="card-tab card-tab--active">Tab 1</button>
  <button class="card-tab">Tab 2</button>
</div>
```

### Empty States
```html
<div class="card-empty-state">
  <Icon class="card-empty-icon" />
  <p class="card-empty-title">No items yet</p>
  <p class="card-empty-description">Description of what to do</p>
  <button class="card-empty-action">Create First</button>
</div>
```

### Error/Delete Banners
```html
<div class="card-error-banner">
  <AlertIcon /> Error message here
</div>

<div class="card-delete-banner">
  <p>Are you sure?</p>
  <div class="button-group">
    <button class="card-btn-secondary">Cancel</button>
    <button class="card-btn-destructive">Delete</button>
  </div>
</div>
```

---

## Mobile Pattern (Snippet-Based)

Use snippets for shared content, conditional wrapper:

```svelte
{#snippet cardContent()}
  <div class="my-card card-system" class:mobile>
    <!-- shared content -->
  </div>
{/snippet}

{#if mobile}
  {@render cardContent()}
{:else}
  <BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
    {@render cardContent()}
  </BaseCard>
{/if}
```

Mobile adjustments:
```css
.my-card {
  border-radius: 0 0 var(--radius-lg) var(--radius-lg);
}

.my-card.mobile {
  border-radius: 0;
  height: 100%;
}
```

---

## Anti-Patterns (Avoid These)

### Don't use hardcoded colors
```css
/* Bad */
background: #1a1a2e;
color: rgb(148, 163, 184);

/* Good */
background: var(--surface-1);
color: var(--text-secondary);
```

### Don't use arbitrary spacing
```css
/* Bad */
padding: 10px 15px;
gap: 7px;

/* Good */
padding: var(--space-3) var(--space-4);
gap: var(--space-2);
```

### Don't duplicate component styles
```css
/* Bad - recreating list item styles */
.my-item {
  display: flex;
  padding: 12px 16px;
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  /* ... many more lines ... */
}

/* Good - use the design system class */
<div class="card-list-item">...</div>
```

### Don't mix font size units
```css
/* Bad */
font-size: 14px;
font-size: 0.875rem;
font-size: 13px;

/* Good - use tokens */
font-size: var(--text-md);
font-size: var(--text-base);
font-size: var(--text-sm);
```

---

## Checklist for Polishing a Card

1. [ ] Imports `./card-design-system.css`
2. [ ] Root element has `card-system` class
3. [ ] All colors use CSS variables
4. [ ] Spacing uses `--space-*` tokens
5. [ ] Typography uses `--text-*` tokens
6. [ ] Uses design system components where applicable (badges, buttons, list items)
7. [ ] Mobile variant handles properly (snippet pattern)
8. [ ] Transitions use `--transition-*` tokens
9. [ ] Shadows use `--shadow-*` tokens
10. [ ] Touch targets are 44x44px minimum on mobile

---

## Card Inventory & Status

| Card | Imports CSS | Uses Tokens | Uses Classes | Polish Status |
|------|-------------|-------------|--------------|---------------|
| BaseCard | No (wrapper) | ✓ | N/A | ✅ Reference - Good |
| ChatCard | ✓ | ✓ | ✓ | ✅ Polished |
| FileBrowserCard | ✗ | Minimal | Minimal | ⚪ Wrapper only - skip |
| HooksCard | ✓ | ✓ | ✓ | ✅ Polished |
| NotesCard | ✓ | ✓ | ✓ | ✅ Fully polished (tokens) |
| PluginManagerCard | ✓ | ✓ | ✓ | ✅ Polished |
| ProfileCard | ✓ | ✓ | ✓ | ✅ Good |
| ProjectCard | ✓ | ✓ | ✓ | ✅ Already good |
| SettingsCard | ✓ | Partial | ✓ | ✅ Polished (large file, uses theme vars) |
| StatsCard | ✓ | ✓ | N/A (custom) | ✅ Good |
| SubagentCard | ✓ | ✓ | ✓ | ✅ Already good |
| UserSettingsCard | ✓ | Partial | ✓ | ✅ Polished |

---

## Polish Priority Order

**Phase 1: Add CSS Import + card-system class** (quick wins)
1. SettingsCard - large, heavily used
2. NotesCard - medium complexity
3. PluginManagerCard - list-based
4. ChatCard - core card
5. UserSettingsCard - smaller

**Phase 2: Light polish** (already imports, needs consistency)
1. HooksCard - clean up inline styles
2. ProjectCard - clean up inline styles
3. SubagentCard - clean up inline styles

**Phase 3: Skip** (special cases)
- FileBrowserCard - just wraps FilesView
- BaseCard - reference implementation
- ProfileCard - already good
- StatsCard - already good
