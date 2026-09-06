<script lang="ts">
 import BrowserPanel from '../src/lib/components/browser/BrowserPanel.svelte';
 import { activeSession } from '../src/lib/stores/active-session';
 import { browserPanel } from '../src/lib/stores/browser-panel.svelte';
 const panel = $derived(browserPanel.forSession($activeSession.sessionId));
</script>
<div class="fixture-shell"><nav aria-label="Global navigation">Shuffle</nav><main class="chat-row"><div class="chat" inert={panel.open && panel.expanded}><button>Chat stays mounted</button></div><BrowserPanel sessionId={$activeSession.sessionId ?? ''} /></main></div>
<style>
 .fixture-shell { height: 100dvh; display: flex; padding-top: 40px; box-sizing: border-box; }
 nav { width: 64px; flex: none; } .chat-row { position: relative; flex: 1; min-width: 0; } .chat { height: 100%; }
 @media (max-width: 600px) { nav { width: 32px; font-size: 8px; } }
</style>
