<script lang="ts">
 import LazyPanel from '../src/lib/components/deck/lazy/LazyPanel.svelte';
 import { BrowserSidePanel } from '../src/lib/components/deck/lazy/panels';
 import { activeSession } from '../src/lib/stores/active-session';
 import { browserPanel } from '../src/lib/stores/browser-panel.svelte';
 // Match MainPanel's lazy prop spread: direct props hide same-ID reconnects.
 const panel = $derived(browserPanel.forSession($activeSession.sessionId));
</script>
<div class="fixture-shell"><nav aria-label="Global navigation">Shuffle</nav><main class="chat-row"><div class="chat" inert={panel.open && panel.expanded}><button>Chat stays mounted</button></div><LazyPanel handle={BrowserSidePanel} props={{ sessionId: $activeSession.sessionId ?? '' }} bare /></main></div>
<style>
 .fixture-shell { height: 100dvh; display: flex; padding-top: 40px; box-sizing: border-box; }
 nav { width: 64px; flex: none; } .chat-row { position: relative; flex: 1; min-width: 0; } .chat { height: 100%; }
 @media (max-width: 600px) { nav { width: 32px; font-size: 8px; } }
</style>
