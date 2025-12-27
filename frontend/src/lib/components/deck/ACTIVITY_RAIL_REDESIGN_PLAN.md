# Activity Rail Redesign Plan
## Casino Noir Edition - Extending Welcome Screen Aesthetics

---

## Executive Summary

This plan details the complete redesign of the left Activity Rail to harmonize with the existing "Casino Noir" design language established in the Welcome Screen. The goal is to transform a functional but generic navigation rail into a distinctive, immersive component that feels like it belongs in an upscale Vegas dealer's console.

---

## Current State Analysis

### Existing ActivityRail.svelte
- **Width:** 64px (functional but feels cramped)
- **Background:** Basic glass morphism (`color-mix(in srgb, var(--card) 85%, transparent)`)
- **Plus Button:** Simple gradient (blue to purple) - generic
- **Activity Buttons:** Plain icons with colored active states
- **Active Indicator:** Basic 3px vertical bar with glow
- **Styling:** Functional but lacks the personality of the welcome screen

### Welcome Screen Design Language (Reference)
- **Background:** Deep felt texture with spotlight gradients
- **Color System:** OKLch-based gold accent system with glows
- **Typography:** Cinzel display + system sans
- **Elements:** Art deco ornaments, playing card aesthetics
- **Animations:** Cubic-bezier bounces, staggered reveals, 3D perspective
- **Cards:** Suit symbols, gold borders on hover, dramatic shadows

---

## Design Vision

### Concept: "The Dealer's Console"
Transform the activity rail into what you'd see on a high-end casino dealer's workstation - elegant, functional, with touches of gold and velvet.

### Key Design Pillars

1. **The Dealer's Chip** - Logo/New button becomes a casino chip
2. **Neon Edge Glow** - Active indicators pulse like casino signage
3. **Art Deco Elegance** - Subtle gold line ornaments
4. **Playing Card Integration** - Suit symbols as subtle watermarks
5. **Felt Depth** - Darker, richer background than current

---

## Detailed Component Specifications

### 1. Rail Container

```css
/* From current */
width: 64px;
background: color-mix(in srgb, var(--card) 85%, transparent);

/* To redesigned */
width: 76px;  /* Slightly wider for breathing room */
background: linear-gradient(180deg,
  var(--felt-deep) 0%,     /* Darker at edges */
  var(--felt) 15%,
  var(--felt) 85%,
  var(--felt-deep) 100%
);
```

**New Features:**
- Art deco line ornaments at top and bottom (gold, 40px wide, low opacity)
- Deeper shadow on right edge
- Subtle inner border glow

### 2. Logo Chip (Plus Button)

**Current:** Generic rounded square with gradient
**Redesigned:** Full casino chip aesthetic

```
Structure:
┌─────────────────┐
│  ╭───────────╮  │  <- Outer ring (gold gradient)
│  │  ╭─────╮  │  │  <- Middle ring (gold border)
│  │  │  +  │  │  │  <- Inner disc (felt + gold icon)
│  │  ╰─────╯  │  │
│  ╰───────────╯  │
└─────────────────┘
```

**Specifications:**
- Outer: 52x52px, radial gold gradient with highlights
- Inner disc: 38x38px, felt-deep background
- Border: 3px solid gold-dim + 2px inner gold
- Shadow: Inset highlights + outer glow
- Hover: Scale 1.1, rotate 15deg, enhanced glow
- Active: Scale 0.95, quick spring back

### 3. Activity Buttons

**Layout per button:**
- Height: 56px (from 48px - more presence)
- Full width with 8px padding
- Rounded corners: 10px

**States:**

| State | Background | Icon Color | Effects |
|-------|-----------|------------|---------|
| Default | transparent | foreground-muted | none |
| Hover | 8% activity color | foreground | subtle bg |
| Active | 15%→8% gradient | activity color | neon edge + suit watermark |

**Active Indicator Evolution:**
```css
/* Current */
width: 3px;
height: 24px;
box-shadow: 0 0 12px var(--activity-color);

/* Redesigned */
width: 3px;
height: 28px;
box-shadow:
  0 0 12px var(--activity-color),
  0 0 24px color-mix(in srgb, var(--activity-color) 60%, transparent),
  4px 0 16px color-mix(in srgb, var(--activity-color) 40%, transparent);
animation: pulseGlow 2s ease-in-out infinite;
```

**Suit Watermarks (Active State):**
- Position: top-right corner, 8px offset
- Font-size: 10px
- Color: activity color at 40% opacity
- Content based on mode:
  - Workspace: ♠ (Spade)
  - Studio: ♥ (Heart)
  - Files: ♦ (Diamond)

### 4. Icon Wrappers

**Active state enhancement:**
```css
.icon-wrapper (active) {
  background: color-mix(in srgb, var(--activity-color) 20%, transparent);
  box-shadow: 0 0 16px color-mix(in srgb, var(--activity-color) 30%, transparent);
}

.icon (active) {
  filter: drop-shadow(0 0 6px var(--activity-color));
}
```

### 5. Tooltips

**Redesigned for elegance:**
```css
.tooltip {
  background: var(--glass-dark);  /* Darker, richer */
  backdrop-filter: blur(12px);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 14px;
  font-weight: 500;

  /* Gold accent line on left */
  box-shadow:
    inset 3px 0 0 var(--gold),
    0 4px 20px rgba(0, 0, 0, 0.3);
}
```

**Animation:** Slide in from left with opacity fade

### 6. Rail Divider

**New element - Art deco separator:**
```css
.rail-divider {
  width: 50px;
  height: 1px;
  margin: 12px 0;
  background: linear-gradient(90deg,
    transparent 0%,
    var(--gold-dim) 20%,
    var(--gold) 50%,
    var(--gold-dim) 80%,
    transparent 100%
  );
  opacity: 0.4;
}
```

### 7. Bottom Section (Settings/Logout)

**Enhanced styling:**
- Settings: Standard muted style
- Logout: Hover reveals subtle red tint (matches card-heart color)
- Same tooltip styling as activity buttons

---

## Color System Additions

Add to `app.css`:

```css
:root {
  /* Existing... */

  /* Enhanced Casino Noir Palette */
  --felt-deep: oklch(0.08 0.02 145);
  --gold-dim: oklch(0.58 0.12 85);
  --gold-bright: oklch(0.82 0.18 85);

  /* Card Suit Colors (for potential future use) */
  --card-spade: #1e293b;
  --card-heart: #dc2626;
  --card-diamond: #f59e0b;
  --card-club: #059669;
}
```

---

## Mobile Adaptation

### Current
- Horizontal bar at bottom, 64px height
- Same plus button styling
- Active indicator moves to bottom

### Redesigned

**Chip Adaptation:**
- Smaller (44x44px) but same aesthetic
- Positioned on left side of rail

**Active Indicator:**
- Horizontal bar below icon (24px wide, 3px tall)
- Same glow effect, horizontal orientation

**No tooltips** - Touch devices don't need hover states

---

## Animation Specifications

### Plus Chip Hover
```css
transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
transform: scale(1.1) rotate(15deg);
```

### Active Indicator Pulse
```css
@keyframes pulseGlow {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}
animation: pulseGlow 2s ease-in-out infinite;
```

### Tooltip Slide-In
```css
/* Default */
opacity: 0;
transform: translateY(-50%) translateX(-4px);

/* Visible */
opacity: 1;
transform: translateY(-50%) translateX(0);
transition: all 0.2s ease;
```

---

## Typography

### Current
- System fonts throughout

### Redesigned
- **Tooltips:** DM Sans 500 (or system-ui fallback)
- No display font needed in rail (too small for Cinzel)

---

## Implementation Steps

### Phase 1: CSS Variables & Foundation
1. Add new color variables to `app.css`
2. Update rail container background
3. Increase width to 76px
4. Add art deco ornament pseudo-elements

### Phase 2: Logo Chip
1. Create new chip structure (nested divs for rings)
2. Apply radial gradient and shadows
3. Implement hover/active animations
4. Test mobile scaling

### Phase 3: Activity Buttons
1. Increase button height to 56px
2. Update active indicator with enhanced glow
3. Add suit watermark pseudo-element for active state
4. Implement icon wrapper glow effect

### Phase 4: Tooltips & Divider
1. Restyle tooltips with gold accent
2. Add slide-in animation
3. Insert rail-divider element
4. Style with gradient

### Phase 5: Mobile Adaptation
1. Adjust chip sizing
2. Horizontal active indicator
3. Test on various viewport sizes
4. Verify touch targets (44px minimum)

### Phase 6: Polish & Testing
1. Verify all animations respect `prefers-reduced-motion`
2. Test with screen readers (aria labels)
3. Performance audit (no layout shifts)
4. Cross-browser testing

---

## File Changes Summary

| File | Changes |
|------|---------|
| `app.css` | Add `--felt-deep`, `--gold-dim`, `--gold-bright`, card suit colors |
| `ActivityRail.svelte` | Complete style overhaul, new HTML structure for chip |
| `types.ts` | Add suit mapping type (optional) |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Performance hit from shadows/filters | Use GPU-accelerated properties only |
| Accessibility contrast issues | Maintain WCAG AA ratios |
| Mobile touch target too small | Enforce 44px minimum |
| Animation jank | Test on low-end devices, use `will-change` sparingly |

---

## Success Criteria

1. **Visual Cohesion:** Rail feels like natural extension of welcome screen
2. **Functionality Preserved:** All existing features work identically
3. **Performance:** No perceptible lag on interaction
4. **Accessibility:** Passes automated a11y audit
5. **Delight Factor:** Users notice and appreciate the upgrade

---

## Mockup Location

Interactive HTML mockup created at:
```
frontend/src/lib/components/deck/activity-rail-redesign-mockup.html
```

Open in browser to preview the full design with both desktop and mobile views.

---

**Design Lead:** Claude (Frontend Design Skill)
**Created:** December 2024
**Status:** Ready for Review
