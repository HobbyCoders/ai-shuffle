<script lang="ts">
 import ContextUsageBar from '../src/lib/components/chat/ContextUsageBar.svelte';
 import Sidebar from '../src/lib/components/layout/Sidebar.svelte';
 import BuildPage from '../src/lib/components/layout/BuildPage.svelte';
 import InstallPrompt from '../src/lib/components/InstallPrompt.svelte';
 let { remote, reset, install }: { remote: () => Promise<string>; reset: () => Promise<string>; install: () => void } = $props();
 let status = $state('Ready — production components with a controlled local transport.');
 const noop = () => {};
</script>
<header>
 <h1>Preference presentation verification</h1>
 <button onclick={async () => status = await remote()}>Other device: collapse and select Hooks</button>
 <button onclick={async () => status = await reset()}>Switch to a fresh account</button>
 <button onclick={install}>Offer synthetic install event</button>
 <p role="status">{status}</p>
</header>
<div class="fixture-layout">
 <aside><Sidebar onNewSession={noop} onSettledOpenSession={noop} onOpenSession={noop} onOpenFeature={noop} /></aside>
 <main><ContextUsageBar /><BuildPage /></main>
</div>
<InstallPrompt />
<style>
 header { padding: 16px; border-bottom: 1px solid var(--border); }
 h1 { font-size: 18px; margin-bottom: 12px; }
 button { padding: 8px 12px; margin: 0 8px 8px 0; border: 1px solid var(--border); border-radius: 6px; }
 .fixture-layout { display: flex; height: 650px; }
 aside { width: 300px; flex-shrink: 0; }
 main { min-width: 0; flex: 1; padding: 18px; }
 @media(max-width: 600px) { .fixture-layout { flex-direction: column; height: auto; } aside { width: 100%; height: 420px; } main { min-height: 500px; } }
</style>
