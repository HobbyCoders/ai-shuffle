# Activity Panel Casino Noir Redesign

## Overview

Transform the existing ActivityPanel into a cohesive Casino Noir experience that matches the CreateMenu "deal new cards" aesthetic. The panel becomes **"The Hand"** - a metaphor for the cards (sessions) you're currently holding.

**Mockup Location:** `frontend/src/lib/components/deck/activity-panel-casino-noir-mockup.html`

---

## Design Philosophy

### The Casino Noir Aesthetic

The CreateMenu establishes these core design principles that we'll extend to the Activity Panel:

1. **Felt Table Foundation** - Deep teal-green gradients (`--felt`, `--felt-deep`) as base
2. **Gold Accent System** - Warm gold tones for highlights, borders, and interactive states
3. **Playing Card Motifs** - Suit symbols (♠ ♦ ♥ ♣) as watermarks and decorations
4. **Art Deco Ornaments** - Corner brackets, centered dividers with gradient lines
5. **Card Stack Metaphor** - Items styled as layered playing cards
6. **Glass Morphism + Depth** - Subtle blur, layered shadows, embossed effects
7. **Spring Animations** - `cubic-bezier(0.34, 1.56, 0.64, 1)` for satisfying interactions

### Naming Convention

| Current | Casino Noir |
|---------|-------------|
| Activity Panel | "The Hand" |
| Active Sessions | "Cards in Play" |
| Threads Tab | "The Stack" |
| Agents Tab | "The Crew" |
| Studio Tab | "The Gallery" |
| Info Tab | "The Score" |

---

## Visual Design Spec

### Panel Container

```css
.activity-panel {
  /* Felt table gradient */
  background: linear-gradient(180deg,
    var(--felt-deep) 0%,
    var(--felt) 8%,
    var(--felt) 92%,
    var(--felt-deep) 100%
  );

  /* Subtle border */
  border-left: 1px solid oklch(0.25 0.02 180 / 0.4);

  /* Elevated shadow */
  box-shadow:
    -2px 0 20px rgba(0, 0, 0, 0.3),
    inset 1px 0 0 oklch(1 0 0 / 0.03);
}

/* Art deco corner ornaments */
.activity-panel::before,
.activity-panel::after {
  content: '';
  position: absolute;
  width: 24px;
  height: 24px;
  border-style: solid;
  border-color: var(--gold-dim);
  opacity: 0.5;
}

.activity-panel::before {
  top: 8px;
  left: 8px;
  border-width: 2px 0 0 2px;
}

.activity-panel::after {
  top: 8px;
  right: 8px;
  border-width: 2px 2px 0 0;
}
```

### Panel Header (New)

Replace the generic header with a Casino Noir title treatment:

```html
<div class="panel-header">
  <h2 class="panel-title">The Hand</h2>
</div>
```

```css
.panel-title {
  font-family: var(--font-display); /* Playfair Display */
  font-size: 1.125rem;
  font-weight: 600;
  letter-spacing: 0.03em;
  text-align: center;
}

/* Decorative underline */
.panel-title::after {
  content: '';
  display: block;
  width: 80px;
  height: 1px;
  margin: 12px auto 0;
  background: linear-gradient(90deg,
    transparent 0%,
    var(--gold) 30%,
    var(--gold) 70%,
    transparent 100%
  );
}
```

### Active Sessions (Cards in Play)

Transform session items into playing cards:

```css
.session-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;

  /* Card background */
  background: linear-gradient(135deg,
    var(--menu-card-bg) 0%,
    oklch(0.12 0.01 260) 100%
  );
  border: 1px solid var(--menu-card-border);
  border-radius: 10px;

  /* Card depth */
  box-shadow:
    0 2px 8px rgba(0, 0, 0, 0.2),
    0 1px 2px rgba(0, 0, 0, 0.15),
    inset 0 1px 0 rgba(255, 255, 255, 0.03);
}

/* Suit watermark */
.session-card::before {
  content: attr(data-suit);
  position: absolute;
  top: 6px;
  right: 10px;
  font-size: 14px;
  color: var(--gold);
  opacity: 0.15;
  transition: opacity 0.2s ease;
}

/* Hover state - card slides right */
.session-card:hover {
  background: linear-gradient(135deg,
    oklch(0.18 0.015 260) 0%,
    oklch(0.14 0.01 260) 100%
  );
  border-color: var(--gold-dim);
  transform: translateX(4px);
  box-shadow:
    0 4px 16px rgba(0, 0, 0, 0.3),
    0 0 16px var(--gold-glow);
}

.session-card:hover::before {
  opacity: 0.4;
}

/* Selected card - gold indicator */
.session-card.selected {
  border-color: var(--gold);
}

.session-card.selected::after {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 24px;
  background: var(--gold);
  border-radius: 0 2px 2px 0;
  box-shadow: 0 0 8px var(--gold-glow);
}
```

### Session Icon Treatment

```css
.session-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;

  /* Gold-tinted container */
  background: color-mix(in srgb, var(--gold) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--gold) 20%, transparent);
}

.session-icon svg {
  stroke: var(--gold);
  stroke-width: 1.5;
}

/* Streaming animation */
.session-icon.streaming svg {
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
```

### Tab Navigation

Style tabs as mini playing cards:

```css
.tab-btn {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 10px 8px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--muted-foreground);
  transition: all 0.2s ease;
  position: relative;
}

/* Suit watermark on tabs */
.tab-btn::before {
  content: attr(data-suit);
  position: absolute;
  top: 4px;
  right: 6px;
  font-size: 8px;
  opacity: 0;
  color: var(--gold);
}

.tab-btn:hover {
  color: var(--foreground);
  background: color-mix(in srgb, var(--gold) 5%, transparent);
  border-color: color-mix(in srgb, var(--gold) 15%, transparent);
}

.tab-btn:hover::before {
  opacity: 0.4;
}

.tab-btn.active {
  color: var(--gold);
  background: linear-gradient(135deg,
    color-mix(in srgb, var(--gold) 10%, transparent) 0%,
    color-mix(in srgb, var(--gold) 5%, transparent) 100%
  );
  border-color: color-mix(in srgb, var(--gold) 25%, transparent);
}

/* Active tab indicator */
.tab-btn.active::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%);
  width: 24px;
  height: 2px;
  background: var(--gold);
  border-radius: 2px 2px 0 0;
  box-shadow: 0 0 8px var(--gold-glow);
}

/* Tab badge */
.tab-badge {
  background: var(--success);
  color: var(--felt-deep);
  font-weight: 700;
}
```

### Section Labels

Art deco styled labels with decorative lines:

```css
.section-label {
  font-size: 0.625rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--gold-dim);
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-label::before,
.section-label::after {
  content: '';
  flex: 1;
  height: 1px;
  background: linear-gradient(90deg,
    transparent 0%,
    var(--gold-dim) 100%
  );
  opacity: 0.3;
}

.section-label::after {
  background: linear-gradient(90deg,
    var(--gold-dim) 0%,
    transparent 100%
  );
}
```

### History Items

Playing card-styled history entries:

```css
.history-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: linear-gradient(135deg,
    oklch(0.13 0.01 260) 0%,
    oklch(0.11 0.008 260) 100%
  );
  border: 1px solid oklch(0.22 0.01 260);
  border-radius: 8px;
  position: relative;
}

.history-item::before {
  content: attr(data-suit);
  position: absolute;
  top: 4px;
  right: 8px;
  font-size: 10px;
  color: var(--gold);
  opacity: 0.12;
}

.history-item:hover {
  border-color: var(--gold-dim);
  transform: translateX(3px);
}

.history-item:hover::before {
  opacity: 0.35;
}
```

### Studio Tab - Generation Grid

Gallery-style cards for image/video generations:

```css
.generation-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}

.generation-card {
  aspect-ratio: 1;
  background: linear-gradient(135deg,
    var(--card-bg) 0%,
    oklch(0.11 0.01 260) 100%
  );
  border: 1px solid var(--card-border);
  border-radius: 10px;
  overflow: hidden;
  position: relative;
}

.generation-card::before {
  content: attr(data-suit);
  position: absolute;
  top: 6px;
  right: 8px;
  font-size: 12px;
  color: var(--gold);
  opacity: 0.2;
  z-index: 2;
}

.generation-card:hover {
  border-color: var(--gold-dim);
  transform: translateY(-2px);
  box-shadow:
    0 8px 20px rgba(0, 0, 0, 0.3),
    0 0 12px var(--gold-glow);
}

/* Generating status badge */
.generation-status.generating {
  color: var(--gold);
}

.generation-status.generating::before {
  content: '';
  width: 5px;
  height: 5px;
  background: var(--gold);
  border-radius: 50%;
  animation: pulse 1s ease-in-out infinite;
}
```

### Info Tab - Stat Cards

Playing card-styled statistics:

```css
.stat-card {
  padding: 14px;
  background: linear-gradient(135deg,
    var(--card-bg) 0%,
    oklch(0.11 0.01 260) 100%
  );
  border: 1px solid var(--card-border);
  border-radius: 10px;
  position: relative;
}

.stat-card::before {
  content: attr(data-suit);
  position: absolute;
  top: 8px;
  right: 10px;
  font-size: 16px;
  color: var(--gold);
  opacity: 0.12;
}

.stat-label {
  font-size: 0.625rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted-foreground);
}

.stat-value {
  font-family: var(--font-display);
  font-size: 1.375rem;
  font-weight: 600;
  color: var(--gold);
}

/* Context usage bar */
.context-bar {
  height: 6px;
  background: oklch(0.2 0.01 260);
  border-radius: 3px;
  overflow: hidden;
}

.context-fill {
  height: 100%;
  background: linear-gradient(90deg,
    var(--gold-dim) 0%,
    var(--gold) 50%,
    var(--gold-bright) 100%
  );
  border-radius: 3px;
}
```

---

## Animation Spec

### Card Hover

```css
.session-card {
  transition:
    background 0.15s ease,
    border-color 0.15s ease,
    box-shadow 0.15s ease,
    transform 0.15s ease;
}

.session-card:hover {
  transform: translateX(4px);
}

.session-card:active {
  transform: translateX(2px) scale(0.98);
  transition-duration: 0.08s;
}
```

### Tab Switch

```css
.tab-btn::after {
  transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  transform-origin: center;
}

/* Tab indicator grows from center */
.tab-btn.active::after {
  animation: tabIndicator 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
}

@keyframes tabIndicator {
  0% {
    width: 0;
    opacity: 0;
  }
  100% {
    width: 24px;
    opacity: 1;
  }
}
```

### Session Status Pulse

```css
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

@keyframes glow {
  0%, 100% {
    box-shadow: 0 0 6px var(--status-streaming);
  }
  50% {
    box-shadow: 0 0 12px var(--status-streaming);
  }
}

.status-dot.streaming {
  animation: glow 1.5s ease-in-out infinite;
}
```

---

## Component Changes

### Files to Modify

| File | Changes |
|------|---------|
| `ActivityPanel.svelte` | Add corner ornaments, felt gradient, panel title |
| `ActivityHeader.svelte` | Restyle as "Cards in Play" with card-stack metaphor |
| `ActivityTabs.svelte` | Add suit watermarks, gold active states |
| `ThreadsTabContent.svelte` | Playing card styled history items |
| `AgentsTabContent.svelte` | Card-styled agent entries |
| `StudioTabContent.svelte` | Generation grid with card aesthetics |
| `InfoTabContent.svelte` | Stat cards with suit decorations |

### New CSS Variables Needed

Add to `app.css` if not present:

```css
:root {
  /* Ensure these exist */
  --font-display: 'Playfair Display', Georgia, serif;

  /* Status colors matching theme */
  --status-streaming: var(--primary);
  --status-active: var(--success);
  --status-idle: var(--muted-foreground);
  --status-error: var(--destructive);
}
```

---

## Implementation Phases

### Phase 1: Foundation (2-3 hours)
1. Update `ActivityPanel.svelte` with corner ornaments and felt gradient
2. Add panel header with "The Hand" title
3. Update base container styling

### Phase 2: Active Sessions (2-3 hours)
1. Restyle `ActivityHeader.svelte` session cards
2. Add suit watermarks via `data-suit` attributes
3. Implement gold-tinted icon containers
4. Add selected state with gold indicator

### Phase 3: Tab Navigation (1-2 hours)
1. Update `ActivityTabs.svelte` with card-style tabs
2. Add suit watermarks to each tab
3. Implement gold active indicator

### Phase 4: Tab Content (3-4 hours)
1. Update `ThreadsTabContent.svelte` with history card styling
2. Update `StudioTabContent.svelte` with generation grid
3. Update `InfoTabContent.svelte` with stat cards
4. Add section labels with decorative lines

### Phase 5: Polish (1-2 hours)
1. Fine-tune animations
2. Test responsive behavior
3. Verify accessibility (color contrast, focus states)
4. Test with actual data

---

## Accessibility Notes

- Maintain WCAG 2.1 AA color contrast (gold on dark = ~7:1 ratio)
- Preserve keyboard navigation
- Keep focus indicators visible (gold ring)
- Ensure animations respect `prefers-reduced-motion`
- Maintain semantic HTML structure

---

## Preview

Open the mockup in browser:
```
frontend/src/lib/components/deck/activity-panel-casino-noir-mockup.html
```

Use the demo controls at bottom-left to switch between tabs.
