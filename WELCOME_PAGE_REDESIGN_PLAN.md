# Welcome Page Redesign Plan: "The Dealer's Table"

> **Aesthetic Direction:** Noir Casino meets Mission Control
> **Framework:** Svelte 5 + SvelteKit 2 + Tailwind CSS
> **Target File:** `frontend/src/lib/components/deck/cards/Workspace.svelte`

---

## Overview

Transform the current minimal empty state into an immersive, memorable "casino table" experience that fully exploits "The Deck" naming metaphor. The redesign will feel sophisticated, tactile, and inviting — replacing the sad "your workspace is empty" message with an empowering "what's your opening move?" prompt.

---

## Phase 1: Design System Extensions

### 1.1 New Color Tokens (app.css)

Add casino-inspired colors to the existing OKLCH design system:

```css
:root {
  /* Casino Noir Palette */
  --felt: oklch(0.18 0.04 145);           /* Deep casino green */
  --felt-highlight: oklch(0.22 0.05 145); /* Lighter felt for gradients */
  --spotlight: oklch(0.92 0.05 85);       /* Warm cream/champagne */
  --gold: oklch(0.72 0.15 85);            /* Rich gold accent */
  --gold-glow: oklch(0.72 0.15 85 / 0.3); /* Gold with transparency */
  --card-surface: oklch(0.16 0.01 260);   /* Elevated card background */
  --card-edge: oklch(0.25 0.02 260);      /* Card border/edge */

  /* Spotlight gradient stops */
  --spotlight-center: oklch(0.92 0.05 85 / 0.08);
  --spotlight-mid: oklch(0.92 0.05 85 / 0.03);
  --spotlight-edge: oklch(0.92 0.05 85 / 0);
}

.dark {
  /* Dark theme uses same values - it's already dark-first */
}

.light {
  /* Light theme alternative - refined cream/wood aesthetic */
  --felt: oklch(0.94 0.02 90);            /* Cream felt */
  --felt-highlight: oklch(0.97 0.01 90);
  --spotlight: oklch(0.35 0.05 50);       /* Warm shadow instead */
  --gold: oklch(0.55 0.15 70);            /* Deeper gold for contrast */
}
```

### 1.2 New CSS Utilities (app.css)

```css
/* Felt texture overlay */
.felt-texture {
  background-image:
    radial-gradient(ellipse at center, var(--spotlight-center) 0%, var(--spotlight-mid) 40%, var(--spotlight-edge) 70%),
    url("data:image/svg+xml,..."); /* Noise texture as inline SVG */
  background-blend-mode: overlay, multiply;
}

/* Card 3D transform utilities */
.card-3d {
  transform-style: preserve-3d;
  perspective: 1000px;
}

/* Gold glow effect */
.gold-glow {
  box-shadow:
    0 0 20px var(--gold-glow),
    0 0 40px var(--gold-glow);
}
```

### 1.3 Typography Addition

Add Playfair Display for the dramatic "DECK" headline. Update `app.html` or load via CSS:

```css
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&display=swap');

:root {
  --font-display: 'Playfair Display', Georgia, serif;
}
```

---

## Phase 2: Component Architecture

### 2.1 New Component Structure

Create modular sub-components for maintainability:

```
frontend/src/lib/components/deck/welcome/
├── WelcomeHero.svelte        # Main welcome container with felt background
├── WelcomeTitle.svelte       # Animated "THE DECK" typography
├── WelcomeCards.svelte       # The three action cards (fanned layout)
├── WelcomeCard.svelte        # Individual card component
├── SpotlightEffect.svelte    # Ambient spotlight/particle effect
└── index.ts                  # Barrel export
```

### 2.2 File: `WelcomeHero.svelte`

**Purpose:** Main container with felt texture and spotlight effect

```svelte
<script lang="ts">
  import WelcomeTitle from './WelcomeTitle.svelte';
  import WelcomeCards from './WelcomeCards.svelte';
  import SpotlightEffect from './SpotlightEffect.svelte';

  interface Props {
    onCreateCard: (type: string) => void;
  }

  let { onCreateCard }: Props = $props();
</script>

<div class="welcome-hero">
  <SpotlightEffect />

  <div class="welcome-content">
    <WelcomeTitle />

    <p class="welcome-tagline">What's your opening move?</p>

    <WelcomeCards {onCreateCard} />
  </div>

  <!-- Corner ornaments -->
  <div class="ornament top-left" aria-hidden="true"></div>
  <div class="ornament top-right" aria-hidden="true"></div>
  <div class="ornament bottom-left" aria-hidden="true"></div>
  <div class="ornament bottom-right" aria-hidden="true"></div>
</div>
```

### 2.3 File: `WelcomeTitle.svelte`

**Purpose:** Animated staggered headline with serif "DECK"

```svelte
<script lang="ts">
  import { onMount } from 'svelte';

  let mounted = $state(false);

  onMount(() => {
    // Trigger entrance animation
    requestAnimationFrame(() => mounted = true);
  });

  const letters = ['D', 'E', 'C', 'K'];
</script>

<div class="title-container" class:mounted>
  <span class="title-the">THE</span>
  <div class="title-deck">
    {#each letters as letter, i}
      <span
        class="title-letter"
        style="--delay: {i * 80}ms"
        class:gold={letter === 'D'}
      >
        {letter}
      </span>
    {/each}
  </div>
  <div class="title-divider"></div>
</div>
```

### 2.4 File: `WelcomeCard.svelte`

**Purpose:** Individual 3D-transforming action card

```svelte
<script lang="ts">
  import type { Component } from 'svelte';

  interface Props {
    type: string;
    label: string;
    description: string;
    icon: Component;
    suit: '♠' | '♥' | '♦' | '♣';
    shortcut: string;
    rotation: number;  // -3, 0, or 3 degrees
    index: number;     // For stagger animation
    onclick: () => void;
  }

  let {
    type, label, description, icon: Icon,
    suit, shortcut, rotation, index, onclick
  }: Props = $props();

  let isHovered = $state(false);
  let cardEl: HTMLButtonElement;

  function handleMouseMove(e: MouseEvent) {
    if (!cardEl) return;
    const rect = cardEl.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width - 0.5;
    const y = (e.clientY - rect.top) / rect.height - 0.5;

    cardEl.style.setProperty('--mouse-x', `${x * 10}deg`);
    cardEl.style.setProperty('--mouse-y', `${y * -10}deg`);
  }

  function handleMouseLeave() {
    isHovered = false;
    cardEl?.style.setProperty('--mouse-x', '0deg');
    cardEl?.style.setProperty('--mouse-y', '0deg');
  }
</script>

<button
  bind:this={cardEl}
  class="welcome-card"
  class:hovered={isHovered}
  style="--rotation: {rotation}deg; --index: {index}"
  onmouseenter={() => isHovered = true}
  onmouseleave={handleMouseLeave}
  onmousemove={handleMouseMove}
  {onclick}
>
  <span class="card-suit top-left">{suit}</span>
  <span class="card-suit bottom-right">{suit}</span>

  <div class="card-content">
    <div class="card-icon">
      <Icon size={32} strokeWidth={1.5} />
    </div>
    <span class="card-label">{label}</span>
    <span class="card-description">{description}</span>
  </div>

  <kbd class="card-shortcut">{shortcut}</kbd>
</button>
```

### 2.5 File: `WelcomeCards.svelte`

**Purpose:** Container for the three fanned cards

```svelte
<script lang="ts">
  import { MessageSquare, Bot, Terminal } from 'lucide-svelte';
  import WelcomeCard from './WelcomeCard.svelte';

  interface Props {
    onCreateCard: (type: string) => void;
  }

  let { onCreateCard }: Props = $props();

  const cards = [
    {
      type: 'chat',
      label: 'CHAT',
      description: 'Start a conversation',
      icon: MessageSquare,
      suit: '♠' as const,
      shortcut: '⌘N',
      rotation: -4
    },
    {
      type: 'agent',
      label: 'AGENT',
      description: 'Deploy an AI ally',
      icon: Bot,
      suit: '♥' as const,
      shortcut: '⌘⇧B',
      rotation: 0
    },
    {
      type: 'terminal',
      label: 'TERMINAL',
      description: 'Open command line',
      icon: Terminal,
      suit: '♦' as const,
      shortcut: '⌘T',
      rotation: 4
    }
  ];
</script>

<div class="cards-container">
  {#each cards as card, i}
    <WelcomeCard
      {...card}
      index={i}
      onclick={() => onCreateCard(card.type)}
    />
  {/each}
</div>
```

### 2.6 File: `SpotlightEffect.svelte`

**Purpose:** Ambient floating particles and spotlight glow

```svelte
<script lang="ts">
  import { onMount } from 'svelte';

  let canvas: HTMLCanvasElement;
  let particles: Array<{x: number; y: number; vx: number; vy: number; size: number; opacity: number}> = [];

  onMount(() => {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Initialize particles (dust in spotlight)
    for (let i = 0; i < 30; i++) {
      particles.push({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        vx: (Math.random() - 0.5) * 0.3,
        vy: (Math.random() - 0.5) * 0.3,
        size: Math.random() * 2 + 0.5,
        opacity: Math.random() * 0.3 + 0.1
      });
    }

    function animate() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      particles.forEach(p => {
        p.x += p.vx;
        p.y += p.vy;

        // Wrap around edges
        if (p.x < 0) p.x = canvas.width;
        if (p.x > canvas.width) p.x = 0;
        if (p.y < 0) p.y = canvas.height;
        if (p.y > canvas.height) p.y = 0;

        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(245, 230, 200, ${p.opacity})`;
        ctx.fill();
      });

      requestAnimationFrame(animate);
    }

    animate();

    return () => {
      particles = [];
    };
  });
</script>

<canvas
  bind:this={canvas}
  class="spotlight-canvas"
  width={800}
  height={600}
></canvas>
```

---

## Phase 3: Styling Implementation

### 3.1 WelcomeHero Styles

```css
.welcome-hero {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;

  /* Felt background with gradient */
  background:
    radial-gradient(
      ellipse 80% 60% at 50% 45%,
      var(--spotlight-center) 0%,
      var(--spotlight-mid) 35%,
      var(--spotlight-edge) 70%
    ),
    linear-gradient(
      180deg,
      hsl(var(--felt)) 0%,
      hsl(var(--felt-highlight)) 50%,
      hsl(var(--felt)) 100%
    );

  /* Felt texture noise overlay */
  &::before {
    content: '';
    position: absolute;
    inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%' height='100%' filter='url(%23noise)'/%3E%3C/svg%3E");
    opacity: 0.03;
    pointer-events: none;
    mix-blend-mode: overlay;
  }

  overflow: hidden;
}

.welcome-content {
  position: relative;
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2rem;
  padding: 2rem;
}

.welcome-tagline {
  font-family: var(--font-sans);
  font-size: 1rem;
  font-weight: 400;
  color: hsl(var(--muted-foreground));
  letter-spacing: 0.15em;
  text-transform: uppercase;
  opacity: 0;
  animation: fadeInUp 0.6s ease-out 0.8s forwards;
}

/* Corner ornaments - art deco style */
.ornament {
  position: absolute;
  width: 60px;
  height: 60px;
  opacity: 0.04;
  pointer-events: none;

  &::before, &::after {
    content: '';
    position: absolute;
    background: var(--gold);
  }

  &.top-left {
    top: 2rem;
    left: 2rem;
    &::before { width: 100%; height: 1px; top: 0; }
    &::after { width: 1px; height: 100%; left: 0; }
  }

  &.top-right {
    top: 2rem;
    right: 2rem;
    &::before { width: 100%; height: 1px; top: 0; }
    &::after { width: 1px; height: 100%; right: 0; }
  }

  &.bottom-left {
    bottom: 2rem;
    left: 2rem;
    &::before { width: 100%; height: 1px; bottom: 0; }
    &::after { width: 1px; height: 100%; left: 0; }
  }

  &.bottom-right {
    bottom: 2rem;
    right: 2rem;
    &::before { width: 100%; height: 1px; bottom: 0; }
    &::after { width: 1px; height: 100%; right: 0; }
  }
}
```

### 3.2 WelcomeTitle Styles

```css
.title-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
}

.title-the {
  font-family: var(--font-sans);
  font-size: 0.875rem;
  font-weight: 500;
  letter-spacing: 0.5em;
  color: hsl(var(--muted-foreground));
  opacity: 0;
  transform: translateY(20px);
  transition: all 0.5s ease-out;
}

.title-container.mounted .title-the {
  opacity: 1;
  transform: translateY(0);
}

.title-deck {
  display: flex;
  gap: 0.1em;
}

.title-letter {
  font-family: var(--font-display);
  font-size: clamp(4rem, 12vw, 7rem);
  font-weight: 900;
  color: hsl(var(--foreground));
  opacity: 0;
  transform: translateY(40px);
  transition: all 0.6s cubic-bezier(0.34, 1.56, 0.64, 1);
  transition-delay: var(--delay);

  &.gold {
    color: var(--gold);
    text-shadow: 0 0 40px var(--gold-glow);
  }
}

.title-container.mounted .title-letter {
  opacity: 1;
  transform: translateY(0);
}

.title-divider {
  width: 120px;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent 0%,
    var(--gold) 50%,
    transparent 100%
  );
  opacity: 0;
  transform: scaleX(0);
  transition: all 0.8s ease-out 0.6s;
}

.title-container.mounted .title-divider {
  opacity: 0.6;
  transform: scaleX(1);
}
```

### 3.3 WelcomeCard Styles

```css
.cards-container {
  display: flex;
  gap: 1.5rem;
  perspective: 1000px;
  margin-top: 1rem;
}

.welcome-card {
  --rotation: 0deg;
  --mouse-x: 0deg;
  --mouse-y: 0deg;
  --index: 0;

  position: relative;
  width: 140px;
  height: 200px;
  padding: 1rem;

  background: linear-gradient(
    145deg,
    hsl(var(--card)) 0%,
    hsl(var(--card-surface)) 100%
  );
  border: 1px solid hsl(var(--border));
  border-radius: 12px;

  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;

  cursor: pointer;
  transform-style: preserve-3d;
  transform:
    rotateZ(var(--rotation))
    rotateY(var(--mouse-x))
    rotateX(var(--mouse-y));

  opacity: 0;
  animation: dealCard 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
  animation-delay: calc(0.9s + var(--index) * 0.1s);

  transition:
    transform 0.15s ease-out,
    box-shadow 0.2s ease-out,
    border-color 0.2s ease-out;

  box-shadow:
    0 4px 12px rgba(0, 0, 0, 0.3),
    0 2px 4px rgba(0, 0, 0, 0.2);

  &:hover {
    transform:
      rotateZ(0deg)
      rotateY(var(--mouse-x))
      rotateX(var(--mouse-y))
      translateY(-8px)
      scale(1.02);

    border-color: var(--gold);

    box-shadow:
      0 12px 32px rgba(0, 0, 0, 0.4),
      0 4px 8px rgba(0, 0, 0, 0.3),
      0 0 20px var(--gold-glow);
  }

  &:active {
    transform: translateY(-4px) scale(0.98);
  }
}

@keyframes dealCard {
  from {
    opacity: 0;
    transform:
      translateX(100px)
      rotateZ(calc(var(--rotation) + 15deg))
      scale(0.8);
  }
  to {
    opacity: 1;
    transform: rotateZ(var(--rotation)) scale(1);
  }
}

.card-suit {
  position: absolute;
  font-size: 0.875rem;
  color: var(--gold);
  opacity: 0.5;

  &.top-left {
    top: 0.5rem;
    left: 0.5rem;
  }

  &.bottom-right {
    bottom: 0.5rem;
    right: 0.5rem;
    transform: rotate(180deg);
  }
}

.card-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
}

.card-icon {
  width: 56px;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: hsl(var(--muted) / 0.5);
  color: hsl(var(--foreground));
  transition: all 0.2s ease-out;

  .welcome-card:hover & {
    background: var(--gold-glow);
    color: var(--gold);
  }
}

.card-label {
  font-family: var(--font-sans);
  font-size: 0.875rem;
  font-weight: 600;
  letter-spacing: 0.1em;
  color: hsl(var(--foreground));
}

.card-description {
  font-size: 0.75rem;
  color: hsl(var(--muted-foreground));
  text-align: center;
  line-height: 1.3;
}

.card-shortcut {
  position: absolute;
  bottom: 0.75rem;
  font-family: var(--font-mono);
  font-size: 0.625rem;
  padding: 0.125rem 0.375rem;
  background: hsl(var(--muted) / 0.3);
  border-radius: 4px;
  color: hsl(var(--muted-foreground));
  opacity: 0;
  transition: opacity 0.2s ease-out;

  .welcome-card:hover & {
    opacity: 1;
  }
}
```

### 3.4 SpotlightEffect Styles

```css
.spotlight-canvas {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 100%;
  height: 100%;
  max-width: 800px;
  max-height: 600px;
  opacity: 0.6;
  pointer-events: none;
  z-index: 1;
}
```

---

## Phase 4: Integration

### 4.1 Update Workspace.svelte

Replace the existing empty state block (lines 148-169) with:

```svelte
<script lang="ts">
  // Add import at top
  import WelcomeHero from './welcome/WelcomeHero.svelte';
</script>

<!-- Replace existing empty state -->
{#if sortedCards.length === 0}
  <WelcomeHero {onCreateCard} />
{/if}
```

### 4.2 Update app.css

Add the new CSS variables and utility classes from Phase 1 to the existing design system.

### 4.3 Add Font Import

In `app.html` or `+layout.svelte`, add:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&display=swap" rel="stylesheet">
```

---

## Phase 5: Responsive Adjustments

### 5.1 Mobile Breakpoints

```css
@media (max-width: 640px) {
  .title-letter {
    font-size: 3rem;
  }

  .cards-container {
    flex-direction: column;
    gap: 1rem;
  }

  .welcome-card {
    width: 100%;
    max-width: 200px;
    height: auto;
    min-height: 120px;

    /* Reset rotation on mobile */
    --rotation: 0deg !important;
    animation: fadeInUp 0.4s ease-out forwards;
  }

  .ornament {
    display: none;
  }

  .spotlight-canvas {
    display: none; /* Save resources on mobile */
  }
}

@media (max-width: 400px) {
  .title-letter {
    font-size: 2.5rem;
  }

  .welcome-tagline {
    font-size: 0.75rem;
  }
}
```

---

## Phase 6: Accessibility

### 6.1 Requirements

- All cards are focusable `<button>` elements with proper labels
- Keyboard navigation works (Tab, Enter/Space to activate)
- `prefers-reduced-motion` disables animations
- Sufficient color contrast (WCAG AA minimum)
- Screen reader announces card purposes

### 6.2 Reduced Motion Support

```css
@media (prefers-reduced-motion: reduce) {
  .title-letter,
  .title-the,
  .title-divider,
  .welcome-tagline,
  .welcome-card {
    animation: none;
    opacity: 1;
    transform: none;
  }

  .welcome-card {
    transition: box-shadow 0.2s ease-out, border-color 0.2s ease-out;
  }

  .spotlight-canvas {
    display: none;
  }
}
```

---

## Implementation Checklist

- [ ] **Phase 1:** Add color tokens and CSS variables to `app.css`
- [ ] **Phase 1:** Add Playfair Display font import
- [ ] **Phase 2:** Create `welcome/` component directory
- [ ] **Phase 2:** Implement `WelcomeHero.svelte`
- [ ] **Phase 2:** Implement `WelcomeTitle.svelte`
- [ ] **Phase 2:** Implement `WelcomeCard.svelte`
- [ ] **Phase 2:** Implement `WelcomeCards.svelte`
- [ ] **Phase 2:** Implement `SpotlightEffect.svelte`
- [ ] **Phase 2:** Create barrel export `index.ts`
- [ ] **Phase 3:** Add all component styles
- [ ] **Phase 4:** Update `Workspace.svelte` to use new components
- [ ] **Phase 5:** Test and refine responsive breakpoints
- [ ] **Phase 6:** Verify accessibility compliance
- [ ] **Phase 6:** Test with `prefers-reduced-motion`
- [ ] **Final:** Cross-browser testing (Chrome, Firefox, Safari, Edge)
- [ ] **Final:** Performance audit (Lighthouse)

---

## Files to Create/Modify

| Action | File Path |
|--------|-----------|
| MODIFY | `frontend/src/app.css` |
| MODIFY | `frontend/src/app.html` |
| CREATE | `frontend/src/lib/components/deck/welcome/WelcomeHero.svelte` |
| CREATE | `frontend/src/lib/components/deck/welcome/WelcomeTitle.svelte` |
| CREATE | `frontend/src/lib/components/deck/welcome/WelcomeCard.svelte` |
| CREATE | `frontend/src/lib/components/deck/welcome/WelcomeCards.svelte` |
| CREATE | `frontend/src/lib/components/deck/welcome/SpotlightEffect.svelte` |
| CREATE | `frontend/src/lib/components/deck/welcome/index.ts` |
| MODIFY | `frontend/src/lib/components/deck/cards/Workspace.svelte` |

---

## Estimated Effort

| Phase | Time Estimate |
|-------|---------------|
| Phase 1: Design System | 30 min |
| Phase 2: Components | 2-3 hours |
| Phase 3: Styling | 1-2 hours |
| Phase 4: Integration | 30 min |
| Phase 5: Responsive | 45 min |
| Phase 6: Accessibility | 30 min |
| **Total** | **5-7 hours** |

---

## Success Criteria

1. **Visual Impact:** First-time visitors have a "wow" moment
2. **Brand Cohesion:** The "Deck" metaphor is immediately clear
3. **Interaction Quality:** Cards feel tactile and satisfying to interact with
4. **Performance:** No jank, smooth 60fps animations
5. **Accessibility:** Full keyboard and screen reader support
6. **Responsiveness:** Beautiful on all screen sizes

---

## Preview Mockup

```
┌──────────────────────────────────────────────────────────────────┐
│ ·                                                              · │
│                                                                  │
│                         ░░░░░░░░░░░░░                            │
│                      ░░░░ spotlight ░░░░                         │
│                                                                  │
│                            T H E                                 │
│                                                                  │
│                    ██████╗ ███████╗ ██████╗██╗  ██╗              │
│                    ██╔══██╗██╔════╝██╔════╝██║ ██╔╝              │
│                    ██║  ██║█████╗  ██║     █████╔╝               │
│                    ██║  ██║██╔══╝  ██║     ██╔═██╗               │
│                    ██████╔╝███████╗╚██████╗██║  ██╗              │
│                    ╚═════╝ ╚══════╝ ╚═════╝╚═╝  ╚═╝              │
│                        ────────────────                          │
│                    WHAT'S YOUR OPENING MOVE?                     │
│                                                                  │
│            ┌─────────┐  ┌─────────┐  ┌─────────┐                │
│           /│ ♠       │  │ ♥       │  │ ♦       │\               │
│          / │  CHAT   │  │  AGENT  │  │TERMINAL │ \              │
│          \ │         │  │         │  │         │ /              │
│           \│   ⌘N    │  │   ⌘⇧B   │  │   ⌘T    │/               │
│            └─────────┘  └─────────┘  └─────────┘                │
│                                                                  │
│ ·                                                              · │
└──────────────────────────────────────────────────────────────────┘

         [casino felt texture + dust particles floating]
```

---

**Ready to implement upon approval.**
