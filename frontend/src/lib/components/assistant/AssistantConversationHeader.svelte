<script lang="ts">
 import { onMount } from 'svelte';
 import { Settings2 } from 'lucide-svelte';
 import { assistantState, refreshAssistant } from '$lib/stores/assistant';
 import { isAdmin } from '$lib/stores/auth.svelte';
 import { layout } from '$lib/stores/layout.svelte';
 import AgentAvatar from './AgentAvatar.svelte';
 let { sessionId, thinking = false }: { sessionId: string | null; thinking?: boolean } = $props();
 const ownerSession = $derived(sessionId);
 const assistant = $derived($assistantState?.home_session_id === ownerSession && ownerSession ? $assistantState : null);
 onMount(() => { if ($isAdmin) void refreshAssistant().catch(() => {}); });
</script>
{#if assistant && $isAdmin}
 <div class="assistant-presence" aria-label="Personal assistant conversation">
  <AgentAvatar avatar={assistant.avatar_id} size={38} />
  <div class="identity"><strong>{assistant.assistant_name}</strong><span>{thinking ? 'Working with you…' : 'Your ongoing conversation'}</span></div>
  <button type="button" aria-label="Customize your assistant" title="Customize your assistant" onclick={() => layout.openFeaturePage('assistant', assistant.agent_id)}><Settings2 size={17} /></button>
 </div>
{/if}
<style>
 .assistant-presence { display:flex; align-items:center; gap:11px; margin:12px auto 0; padding:10px 14px; border:1px solid var(--border); border-radius:12px; width:calc(100% - 32px); max-width:800px; flex:none; background:color-mix(in oklch,var(--primary) 3%,var(--background)); }
 .identity { flex:1; min-width:0; }.identity strong { display:block; font-size:13px; font-weight:550; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.identity span { display:block; margin-top:3px; font-size:10px; color:var(--muted-foreground); }
 button { display:grid; place-items:center; width:44px; height:44px; color:var(--muted-foreground); background:transparent; border:0; border-radius:8px; cursor:pointer; transition:background .2s; }
 button:hover { background:var(--accent); color:var(--foreground); } button:focus-visible { outline:2px solid var(--ring); outline-offset:2px; } button:active { transform:translateY(1px); }
 @media(prefers-reduced-motion:reduce) { button { transition:none; } }
</style>
