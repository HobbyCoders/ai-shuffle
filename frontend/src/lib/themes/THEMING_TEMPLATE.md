# Theming Template & Design System Guide

> **Purpose**: This document defines the authoritative rules for using theme variables when building UI components in this application. Follow these patterns to ensure consistent, theme-compatible styling.

---

## Table of Contents

1. [Design Philosophy](#design-philosophy)
2. [Variable Architecture](#variable-architecture)
3. [Color Roles & Usage](#color-roles--usage)
4. [Surface & Depth System](#surface--depth-system)
5. [Interactive States](#interactive-states)
6. [Component Patterns](#component-patterns)
7. [Special Components](#special-components)
8. [Anti-Patterns](#anti-patterns)
9. [Quick Reference](#quick-reference)

---

## Design Philosophy

### Core Principles

1. **Theme Presets Control Everything** - Presets can override colors, fonts, shadows, and radius
2. **Interactive States Use Primary/Accent** - Primary for CTAs, Accent for hover glows
3. **3-Level Surface Depth** - Background → Card → Popover
4. **Strict Rules with Documented Exceptions** - 90% follows patterns, 10% has justified exceptions

### Visual Signature

- Glass morphism surfaces with subtle blur
- **Primary gradient buttons** with glow effects
- **Accent glow borders** on hover (NOT background change)
- Always-visible focus rings for accessibility
- Subtle shadow layering for depth

---

## Variable Architecture

### Core Color Variables

```
┌─────────────────────────────────────────────────────────────────┐
│  SURFACES (Backgrounds)                                         │
├─────────────────────────────────────────────────────────────────┤
│  --background          │ Page/app background (lowest level)     │
│  --card                │ Elevated surfaces (cards, panels)      │
│  --popover             │ Floating elements (dropdowns, modals)  │
│  --muted               │ Subdued backgrounds (inputs, headers)  │
│  --muted-light         │ Lighter muted variant                  │
│  --muted-dark          │ Darker muted variant                   │
├─────────────────────────────────────────────────────────────────┤
│  TEXT                                                           │
├─────────────────────────────────────────────────────────────────┤
│  --foreground          │ Primary text on --background           │
│  --card-foreground     │ Text on --card surfaces                │
│  --popover-foreground  │ Text on --popover surfaces             │
│  --muted-foreground    │ Secondary/subdued text                 │
├─────────────────────────────────────────────────────────────────┤
│  INTERACTIVE (Actions)                                          │
├─────────────────────────────────────────────────────────────────┤
│  --primary             │ Main CTAs, active states, links        │
│  --primary-foreground  │ Text on primary-colored elements       │
│  --secondary           │ Secondary actions, less prominent      │
│  --secondary-foreground│ Text on secondary-colored elements     │
│  --accent              │ Hover glow/border effects              │
│  --accent-foreground   │ Text when accent is used as background │
├─────────────────────────────────────────────────────────────────┤
│  SEMANTIC (Status)                                              │
├─────────────────────────────────────────────────────────────────┤
│  --destructive         │ Delete, errors, dangerous actions      │
│  --success             │ Success states, confirmations          │
│  --warning             │ Caution, pending, attention needed     │
│  --info                │ Informational, neutral highlights      │
├─────────────────────────────────────────────────────────────────┤
│  UI ELEMENTS                                                    │
├─────────────────────────────────────────────────────────────────┤
│  --border              │ Standard dividers and borders          │
│  --border-subtle       │ Very light borders (10% opacity)       │
│  --input               │ Form input backgrounds                 │
│  --ring                │ Focus ring color (usually = primary)   │
└─────────────────────────────────────────────────────────────────┘
```

### Derived Variables (Computed from Core)

These should be defined using `color-mix()` from core variables:

```css
/* Glass effects */
--glass-bg: color-mix(in oklch, var(--card) 85%, transparent);
--glass-border: color-mix(in oklch, var(--border) 40%, transparent);

/* Glow effects */
--glow-color: color-mix(in srgb, var(--primary) 40%, transparent);
--glow-color-soft: color-mix(in srgb, var(--primary) 30%, transparent);

/* Hover overlay */
--hover-overlay: oklch(1 0 0 / 0.1); /* dark mode */
--hover-overlay: oklch(0 0 0 / 0.05); /* light mode */

/* Primary with opacity */
--primary-10: color-mix(in oklch, var(--primary) 10%, transparent);
--primary-20: color-mix(in oklch, var(--primary) 20%, transparent);
--primary-30: color-mix(in oklch, var(--primary) 30%, transparent);

/* Status backgrounds */
--status-success-bg: color-mix(in oklch, var(--success) 15%, transparent);
--status-warning-bg: color-mix(in oklch, var(--warning) 15%, transparent);
--status-error-bg: color-mix(in oklch, var(--destructive) 15%, transparent);
--status-info-bg: color-mix(in oklch, var(--info) 15%, transparent);
```

---

## Color Roles & Usage

### Primary (`--primary`)

**Role**: Main interactive color - CTAs, active states, links, focus indicators

**USE FOR:**
- ✅ Primary buttons (gradient background)
- ✅ Links and clickable text
- ✅ Active/selected state indicators
- ✅ Focus rings (`--ring` typically equals `--primary`)
- ✅ Progress bars and loading indicators
- ✅ Badges highlighting important items
- ✅ Icon highlight when focused/active

**DO NOT USE FOR:**
- ❌ Large background areas
- ❌ Static decorative elements
- ❌ Text body content
- ❌ Borders (except focus states)

```css
/* GOOD */
.primary-button {
  background: var(--button-primary-gradient);
  color: var(--primary-foreground);
}

.active-tab {
  border-bottom-color: var(--primary);
  color: var(--primary);
}

/* BAD */
.card-header {
  background: var(--primary); /* Too prominent! Use --muted */
}
```

### Secondary (`--secondary`)

**Role**: Less prominent interactive elements, secondary actions

**USE FOR:**
- ✅ Secondary/ghost buttons
- ✅ Cancel/dismiss buttons
- ✅ Toolbar backgrounds
- ✅ Toggle button inactive states

**DO NOT USE FOR:**
- ❌ Primary CTAs
- ❌ Active state indicators
- ❌ Links

```css
/* GOOD */
.secondary-button {
  background: var(--secondary);
  color: var(--secondary-foreground);
  border: 1px solid var(--border);
}

/* BAD */
.main-action {
  background: var(--secondary); /* Should be --primary */
}
```

### Accent (`--accent`)

**Role**: Hover effects, glow borders, subtle highlights

**IMPORTANT**: In this system, accent is used for GLOW/BORDER effects on hover, NOT as a background fill.

**USE FOR:**
- ✅ Hover glow borders: `box-shadow: 0 0 0 1px var(--accent)`
- ✅ Hover state accents: `border-color: var(--accent)`
- ✅ Subtle selection indicators
- ✅ Badge/chip backgrounds (with transparency)

**DO NOT USE FOR:**
- ❌ Primary interactive colors (use `--primary`)
- ❌ Large solid backgrounds
- ❌ Text color

```css
/* GOOD - Hover glow effect */
.interactive-item:hover {
  border-color: var(--accent);
  box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 30%, transparent);
}

/* GOOD - Chip background */
.chip {
  background: color-mix(in oklch, var(--accent) 15%, transparent);
}

/* BAD */
.card:hover {
  background: var(--accent); /* Don't fill backgrounds with accent */
}
```

### Muted (`--muted` and variants)

**Role**: Neutral backgrounds for non-interactive elements

**USE FOR:**
- ✅ Card headers
- ✅ Input backgrounds (unfocused)
- ✅ Code blocks
- ✅ Tab bars
- ✅ Sidebar backgrounds
- ✅ Secondary text (`--muted-foreground`)

```css
/* GOOD */
.card-header {
  background: linear-gradient(180deg,
    color-mix(in oklch, var(--muted) 100%, transparent) 0%,
    color-mix(in oklch, var(--muted) 85%, var(--card)) 100%
  );
}

.input {
  background: var(--muted);
}

.secondary-text {
  color: var(--muted-foreground);
}
```

---

## Surface & Depth System

### Three-Level Hierarchy

```
┌─────────────────────────────────────────────────────────────┐
│  Level 0: --background                                      │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Level 1: --card                                      │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Level 2: --popover                             │  │  │
│  │  │  (dropdowns, modals, tooltips)                  │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │  (cards, panels, sidebars)                            │  │
│  └───────────────────────────────────────────────────────┘  │
│  (page background)                                          │
└─────────────────────────────────────────────────────────────┘
```

### When to Use Each Level

| Surface | Variable | Use For |
|---------|----------|---------|
| **Level 0** | `--background` | Page background, app shell |
| **Level 1** | `--card` | Cards, panels, sidebars, content areas |
| **Level 2** | `--popover` | Dropdowns, modals, tooltips, context menus |
| **Glass** | `--glass-bg` | Floating elements that overlay content |

### Glass Morphism

Use glass effects for floating UI that overlays content:

```css
.floating-panel {
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur, 12px));
  -webkit-backdrop-filter: blur(var(--glass-blur, 12px));
  border: 1px solid var(--glass-border);
}
```

---

## Interactive States

### Button States

```css
/* PRIMARY BUTTON */
.btn-primary {
  /* Default */
  background: var(--button-primary-gradient);
  color: var(--primary-foreground);
  box-shadow: var(--shadow-s);

  /* Hover - brighten + glow */
  &:hover {
    filter: brightness(1.1);
    box-shadow: var(--shadow-m), 0 0 20px var(--glow-color-soft);
  }

  /* Active/Pressed */
  &:active {
    transform: scale(0.98);
  }

  /* Focus */
  &:focus-visible {
    outline: 2px solid var(--ring);
    outline-offset: 2px;
  }

  /* Disabled */
  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

/* SECONDARY BUTTON */
.btn-secondary {
  background: var(--secondary);
  color: var(--secondary-foreground);
  border: 1px solid var(--border);

  &:hover {
    background: var(--accent);
    border-color: var(--accent);
  }
}
```

### Interactive List Items

```css
.list-item {
  /* Default */
  background: var(--card);
  border: 1px solid var(--border-subtle);

  /* Hover - accent glow border */
  &:hover {
    border-color: color-mix(in oklch, var(--accent) 50%, var(--border));
    box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 20%, transparent);
  }

  /* Active/Selected */
  &.selected {
    background: color-mix(in oklch, var(--primary) 8%, var(--card));
    border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
  }

  /* Focus */
  &:focus-visible {
    outline: 2px solid var(--ring);
    outline-offset: 2px;
  }
}
```

### Form Inputs

```css
.input {
  /* Default - subtle background */
  background: var(--input-bg);
  border: 1px solid var(--border);
  color: var(--foreground);

  /* Placeholder */
  &::placeholder {
    color: var(--muted-foreground);
  }

  /* Hover */
  &:hover:not(:focus) {
    border-color: var(--input-border-hover);
    background: var(--input-bg-hover);
  }

  /* Focus - primary border + ring */
  &:focus {
    border-color: var(--ring);
    box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 20%, transparent);
    background: var(--input-bg-focus);
  }
}
```

---

## Component Patterns

### Cards

```css
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-m);
}

.card-header {
  /* Use muted for headers - stays neutral across themes */
  background: linear-gradient(180deg,
    color-mix(in oklch, var(--muted) 100%, transparent) 0%,
    color-mix(in oklch, var(--muted) 85%, var(--card)) 100%
  );
  border-bottom: 1px solid var(--border);
}

.card-content {
  background: var(--card);
  color: var(--card-foreground);
}
```

### Badges/Chips

```css
/* Primary badge */
.badge-primary {
  background: color-mix(in oklch, var(--primary) 15%, transparent);
  color: var(--primary);
  border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
}

/* Status badges */
.badge-success {
  background: var(--status-success-bg);
  color: var(--success);
  border: 1px solid var(--status-success-border);
}

.badge-warning {
  background: var(--status-warning-bg);
  color: var(--warning);
  border: 1px solid var(--status-warning-border);
}

.badge-destructive {
  background: var(--status-error-bg);
  color: var(--destructive);
  border: 1px solid var(--status-error-border);
}
```

### Tabs

```css
.tab {
  color: var(--muted-foreground);
  border-bottom: 2px solid transparent;

  &:hover {
    color: var(--foreground);
    background: var(--hover-overlay);
  }

  &.active {
    color: var(--primary);
    border-bottom-color: var(--primary);
  }
}
```

### Dropdowns/Popovers

```css
.dropdown {
  background: var(--popover);
  color: var(--popover-foreground);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-l);
}

.dropdown-item {
  &:hover {
    background: var(--hover-overlay);
  }

  &.selected {
    background: color-mix(in oklch, var(--primary) 10%, transparent);
    color: var(--primary);
  }
}
```

---

## Special Components

### Chat/Messaging UI

```css
/* User message bubble */
.message-user {
  background: color-mix(in oklch, var(--primary) 15%, var(--card));
  border: 1px solid color-mix(in oklch, var(--primary) 25%, transparent);
}

/* AI/Assistant message bubble */
.message-assistant {
  background: var(--card);
  border: 1px solid var(--border);
}

/* Streaming indicator */
.streaming-indicator {
  color: var(--primary);
  animation: pulse 1.5s infinite;
}

/* Tool call badges - use semantic tool colors */
.tool-badge-bash { background: var(--status-success-bg); color: var(--tool-bash); }
.tool-badge-read { background: var(--status-info-bg); color: var(--tool-read); }
.tool-badge-write { background: var(--status-warning-bg); color: var(--tool-write); }
.tool-badge-edit { background: color-mix(in oklch, var(--tool-edit) 15%, transparent); color: var(--tool-edit); }
```

### Terminal/Code

```css
/* Terminal container */
.terminal {
  background: var(--terminal-bg);
  color: var(--terminal-fg);
  font-family: var(--font-mono);
}

/* Use dedicated terminal colors for syntax */
.terminal-red { color: var(--terminal-red); }
.terminal-green { color: var(--terminal-green); }
.terminal-yellow { color: var(--terminal-yellow); }
.terminal-blue { color: var(--terminal-blue); }
.terminal-cyan { color: var(--terminal-cyan); }
.terminal-magenta { color: var(--terminal-magenta); }

/* Code blocks */
.code-block {
  background: var(--muted);
  border: 1px solid var(--border);
  font-family: var(--font-mono);
}

/* Inline code */
.inline-code {
  background: var(--muted);
  border: 1px solid var(--border);
  padding: 0.125rem 0.375rem;
  border-radius: calc(var(--radius) * 0.33);
}
```

### Data Visualization

```css
/* Progress bars */
.progress-bar {
  background: var(--muted);
  border-radius: var(--radius);
}

.progress-fill {
  background: var(--primary);
  /* For multi-color: use chart colors */
}

/* Charts - use chart color variables */
.chart-series-1 { fill: var(--chart-1); stroke: var(--chart-1); }
.chart-series-2 { fill: var(--chart-2); stroke: var(--chart-2); }
.chart-series-3 { fill: var(--chart-3); stroke: var(--chart-3); }
.chart-series-4 { fill: var(--chart-4); stroke: var(--chart-4); }
.chart-series-5 { fill: var(--chart-5); stroke: var(--chart-5); }

/* Stats cards */
.stat-value {
  color: var(--foreground);
  font-family: var(--font-mono);
  font-weight: 700;
}

.stat-label {
  color: var(--muted-foreground);
  font-size: var(--text-xs);
}

.stat-change-positive { color: var(--success); }
.stat-change-negative { color: var(--destructive); }
```

---

## Anti-Patterns

### ❌ Don't Use Hardcoded Colors

```css
/* BAD */
.my-button {
  background: #3b82f6;
  color: white;
}

/* GOOD */
.my-button {
  background: var(--primary);
  color: var(--primary-foreground);
}
```

### ❌ Don't Mix Variable Systems

```css
/* BAD - mixing oklch and hex */
.mixed-element {
  background: oklch(0.5 0.2 240);
  border-color: #e5e7eb;
}

/* GOOD - use variables */
.consistent-element {
  background: var(--primary);
  border-color: var(--border);
}
```

### ❌ Don't Use Primary for Static Elements

```css
/* BAD - primary for decorative background */
.card-header {
  background: var(--primary);
}

/* GOOD - muted for static backgrounds */
.card-header {
  background: var(--muted);
}
```

### ❌ Don't Override Theme Variables with Hardcoded Values

```css
/* BAD */
.dark .my-element {
  background: #1f1f1f;
}

/* GOOD */
.my-element {
  background: var(--card);
}
```

### ❌ Don't Use Accent as a Background Fill

```css
/* BAD - accent as solid background */
.hover-item:hover {
  background: var(--accent);
}

/* GOOD - accent as glow/border effect */
.hover-item:hover {
  border-color: var(--accent);
  box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 25%, transparent);
}
```

---

## Quick Reference

### Variable → Component Mapping

| Component | Background | Text | Border | Active/Focus |
|-----------|------------|------|--------|--------------|
| **Page** | `--background` | `--foreground` | — | — |
| **Card** | `--card` | `--card-foreground` | `--border` | — |
| **Card Header** | `--muted` gradient | `--muted-foreground` | `--border` | — |
| **Primary Button** | `--primary` gradient | `--primary-foreground` | — | `--ring` |
| **Secondary Button** | `--secondary` | `--secondary-foreground` | `--border` | `--ring` |
| **Input** | `--input-bg` | `--foreground` | `--border` | `--ring` |
| **Dropdown** | `--popover` | `--popover-foreground` | `--border` | — |
| **Selected Item** | `--primary` 10% | `--primary` | `--primary` 40% | — |
| **Hover State** | — | — | `--accent` glow | — |
| **Badge (primary)** | `--primary` 15% | `--primary` | `--primary` 30% | — |
| **Badge (success)** | `--success` 15% | `--success` | `--success` 30% | — |
| **Badge (warning)** | `--warning` 15% | `--warning` | `--warning` 30% | — |
| **Badge (error)** | `--destructive` 15% | `--destructive` | `--destructive` 30% | — |
| **Error State** | `--destructive` 10% | `--destructive` | `--destructive` 25% | — |

### State Color Formulas

```css
/* Selected state */
background: color-mix(in oklch, var(--primary) 8-12%, var(--card));
border-color: color-mix(in oklch, var(--primary) 40%, var(--border));

/* Hover glow */
border-color: var(--accent);
box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 25%, transparent);

/* Badge */
background: color-mix(in oklch, var([color]) 12-15%, transparent);
color: var([color]);
border: 1px solid color-mix(in oklch, var([color]) 25-30%, transparent);

/* Focus ring */
outline: 2px solid var(--ring);
outline-offset: 2px;
```

### Typography Variables

| Token | Size | Use Case |
|-------|------|----------|
| `--text-xs` | 11px | Micro labels, timestamps |
| `--text-sm` | 12px | Captions, badges |
| `--text-base` | 13px | Body text |
| `--text-md` | 14px | Emphasized text |
| `--text-lg` | 16px | Section titles |
| `--text-xl` | 18px | Card titles |

### Spacing Variables

| Token | Value | Use Case |
|-------|-------|----------|
| `--space-1` | 4px | Tight gaps |
| `--space-2` | 8px | Standard gap |
| `--space-3` | 12px | Small padding |
| `--space-4` | 16px | Standard padding |
| `--space-5` | 20px | Generous padding |
| `--space-6` | 24px | Section spacing |
| `--space-8` | 32px | Large breaks |

---

## Checklist for New Components

- [ ] Uses CSS variables, no hardcoded colors
- [ ] Background uses appropriate surface level (`--background`, `--card`, `--popover`)
- [ ] Text uses matching foreground variable
- [ ] Interactive elements use `--primary` for active states
- [ ] Hover states use accent glow border, not background fill
- [ ] Focus states have visible `--ring` outline
- [ ] Borders use `--border` or `--border-subtle`
- [ ] Status colors use semantic variables (`--success`, `--warning`, etc.)
- [ ] Badges use opacity-mixed backgrounds (15%) and borders (30%)
- [ ] Spacing uses `--space-*` tokens
- [ ] Typography uses `--text-*` tokens
- [ ] Works in both light and dark modes
- [ ] Works with theme presets (no assumptions about specific colors)
