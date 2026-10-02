<script lang="ts">
 import { onMount } from 'svelte';
 import { ArrowUpRight, Check, RefreshCw } from 'lucide-svelte';
 import { AVATARS, updateAgentIdentity, assistantError, type AvatarId } from '$lib/api/assistant';
 import { assistantState, agentIdentities, refreshAssistant } from '$lib/stores/assistant';
 import { sessionOwnerEpoch } from '$lib/stores/session-owner';
 import { layout } from '$lib/stores/layout.svelte';
 import AgentAvatar from './AgentAvatar.svelte';
 let { agentId, builtin = false }: { agentId: string; builtin?: boolean } = $props();
 const id = $derived(agentId);
 const identity = $derived($agentIdentities.find(a => a.id === id));
 const primary = $derived($assistantState?.agent_id === id);
 let loading = $state(true);
 let saving = $state(false);
 let error = $state('');
 let saved = $state(false);
 const owner = sessionOwnerEpoch();
 async function load() {
  loading = true; error = '';
  try { await refreshAssistant(); } catch(e) { error = assistantError(e); } finally { loading = false; }
 }
 onMount(() => { void load(); });
 async function choose(avatar_id: AvatarId) {
  if (saving || builtin) return;
  const target = id; saving = true; error = ''; saved = false;
  try {
   const result = await updateAgentIdentity(target, { avatar_id });
   if (owner !== sessionOwnerEpoch() || target !== id) return;
   agentIdentities.update(rows => rows.map(row => row.id === target ? result : row));
   assistantState.update(state => state?.agent_id === target ? { ...state, avatar_id, agent: result } : state);
   saved = true;
  } catch(e) { if (owner === sessionOwnerEpoch()) error = assistantError(e); }
  finally { saving = false; }
 }
</script>

<section class="agent-personality" aria-label="Agent avatar and personal assistant">
 <div class="heading"><div><h3>Put a face to the name</h3><p>Shared everywhere this agent appears. Instructions and permissions stay unchanged.</p></div>{#if saved}<span class="saved" role="status"><Check size={13} />Saved</span>{/if}</div>
 {#if loading}<p role="status">Loading agent identity…</p>
 {:else if error}<div role="alert" class="error">{error}<button onclick={load}><RefreshCw size={14} />Retry</button></div>
 {:else}<div class="choices" role="group" aria-label="Agent avatar">{#each AVATARS as avatar}<button type="button" aria-label={`${avatar} avatar`} aria-pressed={(identity?.avatar_id ?? 'robot') === avatar} title={avatar} disabled={builtin || saving} onclick={() => choose(avatar)}><AgentAvatar {avatar} size={42} /></button>{/each}</div>{/if}
 {#if builtin}<p class="hint">Built-in identity is protected. Create a custom agent to personalize its name and avatar.</p>{/if}
 <div class="designation"><div><h4>{primary ? 'Your personal assistant' : 'Personal assistant'}</h4><p>{primary ? 'This agent has your lasting home conversation. Background research and its sources are managed in Assistant.' : 'Give this agent a lasting home conversation. Background research stays off until you turn it on in Assistant.'}</p></div><button type="button" onclick={() => layout.openFeaturePage('assistant', id)}>{primary ? 'Assistant settings' : 'Set up as assistant'}<ArrowUpRight size={14} /></button></div>
</section>

<style>
 .agent-personality { padding:24px; margin:0 0 24px; border:1px solid var(--border); border-radius:12px; background:color-mix(in oklch,var(--primary) 3%,var(--card)); }
 .heading { display:flex; justify-content:space-between; gap:12px; align-items:start; }
 h3 { font-size:15px; font-weight:550; margin:0; } h4 { font-size:12px; font-weight:550; margin:0; }
 p { font-size:12px; color:var(--muted-foreground); line-height:1.7; margin:7px 0 0; }
 .choices { display:grid; grid-template-columns:repeat(4,max-content); gap:8px; margin:18px 0; }
 button { display:inline-flex; align-items:center; gap:8px; min-height:44px; cursor:pointer; color:var(--primary); background:transparent; border:1px solid transparent; border-radius:8px; font-size:11px; transition:background .2s,border-color .2s; }
 .choices button { padding:6px; color:var(--muted-foreground); }
 .choices button[aria-pressed=true] { border-color:var(--primary); color:var(--foreground); }
 button:hover:not(:disabled) { background:var(--accent); } button:active { transform:translateY(1px); }
 button:disabled { opacity:.55; cursor:not-allowed; } button:focus-visible { outline:2px solid var(--ring); outline-offset:3px; }
 .designation { display:flex; align-items:center; gap:20px; justify-content:space-between; border-top:1px solid var(--border); padding-top:18px; margin-top:18px; }
 .designation>div { flex:1; } .designation button { flex:none; }
 .hint,.saved { font-size:11px; color:var(--muted-foreground); }.saved { display:flex; align-items:center; gap:5px; color:var(--primary); }
 .error { font-size:12px; color:var(--destructive); }
 @media(max-width:600px) { .agent-personality { padding:18px; } .choices { gap:4px; }.choices button { padding:5px; } .designation { align-items:flex-start; flex-direction:column; gap:8px; } }
 @media(prefers-reduced-motion:reduce) { button { transition:none; } }
</style>
