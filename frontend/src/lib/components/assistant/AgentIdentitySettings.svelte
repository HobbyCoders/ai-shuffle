<script lang="ts">
 /**
  * AgentIdentitySettings: the avatar picker and the personal-assistant line in
  * the agent editor. The avatar saves on its own through the identity PATCH,
  * never through the agent's full-replace PUT.
  */
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

<section class="aid" aria-label="Agent avatar and personal assistant">
 <div class="aid-field">
  <span class="aid-label">Avatar{#if saved}<span class="aid-saved" role="status"><Check size={12} />Saved</span>{/if}</span>
  {#if loading}<p class="aid-note" role="status">Loading avatars…</p>
  {:else if error}<div role="alert" class="aid-error">{error}<button type="button" class="aid-link" onclick={load}><RefreshCw size={13} />Retry</button></div>
  {:else}<div class="aid-choices" role="group" aria-label="Agent avatar">{#each AVATARS as avatar}<button type="button" class="aid-av" aria-label={`${avatar} avatar`} aria-pressed={(identity?.avatar_id ?? 'robot') === avatar} title={avatar} disabled={builtin || saving} onclick={() => choose(avatar)}><AgentAvatar {avatar} size={28} /></button>{/each}</div>{/if}
  {#if builtin}<p class="aid-note">Built-in avatars are locked.</p>{/if}
 </div>
 <div class="aid-assist">
  <span class="aid-label">{primary ? 'Your personal assistant' : 'Personal assistant'}</span>
  <span class="aid-note">{primary ? 'Home conversation and research live in Assistant.' : 'Give it a lasting home conversation.'}</span>
  <button type="button" class="aid-link" onclick={() => layout.openFeaturePage('assistant', id)}>{primary ? 'Assistant settings' : 'Set up as assistant'}<ArrowUpRight size={13} /></button>
 </div>
</section>

<style>
 .aid { display:flex; flex-direction:column; gap:14px; min-width:0; }
 .aid-field { display:flex; flex-direction:column; gap:6px; min-width:0; }
 .aid-label { display:flex; align-items:center; gap:8px; font-size:12px; font-weight:500; color:var(--muted-foreground); }
 .aid-saved { display:inline-flex; align-items:center; gap:3px; color:var(--primary); font-size:11px; }
 .aid-note { margin:0; font-size:12px; color:var(--muted-foreground); line-height:1.5; }
 .aid-choices { display:flex; flex-wrap:wrap; gap:4px; }
 .aid-av { display:grid; place-items:center; padding:3px; border:1.5px solid transparent; border-radius:999px; background:transparent; cursor:pointer; transition:border-color .15s, background .15s; }
 .aid-av:hover:not(:disabled) { background:var(--hover-overlay, var(--muted)); }
 .aid-av[aria-pressed=true] { border-color:var(--primary); }
 .aid-av:disabled { cursor:not-allowed; opacity:.55; }
 .aid-av[aria-pressed=true]:disabled { opacity:1; }
 .aid-assist { display:flex; align-items:center; flex-wrap:wrap; gap:4px 12px; }
 .aid-link { display:inline-flex; align-items:center; gap:4px; padding:0; border:none; background:none; font:inherit; font-size:12.5px; font-weight:600; color:var(--primary); cursor:pointer; }
 .aid-link:hover { text-decoration:underline; }
 .aid-error { display:flex; align-items:center; gap:10px; font-size:12px; color:var(--destructive); }
 .aid-av:focus-visible, .aid-link:focus-visible { outline:2px solid var(--ring); outline-offset:2px; }
 @media(prefers-reduced-motion:reduce) { .aid-av { transition:none; } }
</style>
