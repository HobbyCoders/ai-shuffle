<script lang="ts">
 /** Assistant is a home for one existing agent, not a new conversation mode. */
 import { onMount } from 'svelte';
 import { ArrowUpRight, MessageSquare, AudioLines, Pause, Play, Settings2, RefreshCw, ShieldCheck, Plus, Check, Activity, ChevronRight } from 'lucide-svelte';
 import { AVATARS, DEFAULT_ASSISTANT_NAME, randomAvatar, configureAssistant, openAssistant, pauseAssistant, resumeAssistant, getAssistantSources, getAssistantActivity, setAssistantSource, assistantError, type AssistantSource, type AssistantActivity, type AssistantPatch, type AvatarId } from '$lib/api/assistant';
 import { assistantState, agentIdentities, acceptAssistant, refreshAssistant } from '$lib/stores/assistant';
 import { appData, profiles } from '$lib/stores/app-data.svelte';
 import { layout } from '$lib/stores/layout.svelte';
 import { sessionOwnerEpoch } from '$lib/stores/session-owner';
 import { openSession } from '$lib/services/session-navigation';
 import { isVoiceEnabled } from '$lib/api/voice';
 import { startCallForSession } from '$lib/services/voiceCall';
 import AgentAvatar from './AgentAvatar.svelte';

 let { requestedAgentId = null }: { requestedAgentId?: string | null } = $props();
 const requestedId = $derived(requestedAgentId);
 const assistant = $derived($assistantState);
 const owner = sessionOwnerEpoch();
 let disposed = false;
 const current = () => !disposed && owner === sessionOwnerEpoch();
 let loading = $state(true);
 let refreshing = $state(false);
 let busy = $state('');
 let error = $state('');
 let notice = $state('');
 let voiceEnabled = $state(false);
 let sources = $state<AssistantSource[]>([]);
 let activity = $state<AssistantActivity[]>([]);
 let editing = $state(false);
 let activating = $state(false);
 // 'dedicated' is the agent setup creates (or already created) for the
 // assistant; 'mine' is any existing agent the owner picks instead.
 let agentChoice = $state<'dedicated' | 'mine'>('dedicated');
 let agentId = $state('');
 let assistantName = $state('');
 let avatar = $state<AvatarId>('robot');
 // Picked once per page so toggling the agent choice does not reshuffle it.
 const suggestedAvatar = randomAvatar();
 const dedicatedAgent = $derived($agentIdentities.find(a => a.dedicated));
 const chosenAgent = $derived($profiles.find(p => p.id === agentId));
 const identityEditable = $derived(agentChoice === 'dedicated' || (!!chosenAgent && !chosenAgent.is_builtin));
 const canSave = $derived(agentChoice === 'dedicated' || !!agentId);
 const hasAgent = $derived(!!assistant?.agent);
 const statusText = $derived(!assistant ? '' : ({ unconfigured: 'Not set up', agent_unavailable: 'Agent unavailable', disabled: 'Background off', paused: 'Background paused', guard_unavailable: 'Research unavailable', ready: 'Background enabled' }[assistant.status]));
 const optedIn = $derived(sources.filter(s => s.enabled).length);

 let loaded = false;
 async function load() {
  if (refreshing) return;
  refreshing = true;
  try {
   const [, nextSources, nextActivity] = await Promise.all([refreshAssistant(), getAssistantSources(), getAssistantActivity()]);
   if (!current()) return;
   sources = nextSources; activity = nextActivity; error = '';
   // Setup opens on any successful load, not just the first: a failed first load
   // followed by a retry or poll must still lead an unconfigured owner into setup.
   const firstLoad = !loaded; loaded = true;
   if (!editing && (!$assistantState?.configured || (firstLoad && requestedId))) {
    beginEdit();
    if (requestedId && $profiles.some(p => p.id === requestedId)) { agentChoice = 'mine'; agentId = requestedId; selectAgent(); }
   }
  } catch (e) { if (current()) error = assistantError(e); }
  finally { if (current()) { loading = false; refreshing = false; } }
 }
 onMount(() => {
  void load();
  void isVoiceEnabled().then(enabled => { if (current()) voiceEnabled = enabled; });
  const tick = setInterval(() => { if (document.visibilityState === 'visible' && !busy && !editing) void load(); }, 15000);
  return () => { disposed = true; clearInterval(tick); };
 });
 function selectAgent() {
  const identity = $agentIdentities.find(a => a.id === agentId);
  assistantName = identity?.name ?? $profiles.find(p => p.id === agentId)?.name ?? '';
  avatar = identity?.avatar_id ?? 'robot';
 }
 function selectDedicated() {
  assistantName = dedicatedAgent?.name ?? DEFAULT_ASSISTANT_NAME;
  avatar = dedicatedAgent?.avatar_id ?? suggestedAvatar;
 }
 function chooseAgentKind(kind: 'dedicated' | 'mine') {
  agentChoice = kind;
  if (kind === 'dedicated') selectDedicated();
  else if (agentId) selectAgent();
 }
 function beginEdit(activate = false) {
  if (!assistant) return;
  if (assistant.agent_id) {
   agentChoice = assistant.agent?.dedicated ? 'dedicated' : 'mine';
   agentId = assistant.agent?.dedicated ? '' : assistant.agent_id;
   assistantName = assistant.assistant_name ?? '';
   avatar = assistant.avatar_id;
  } else {
   agentChoice = 'dedicated'; agentId = '';
   selectDedicated();
  }
  activating = activate; editing = true; error = ''; notice = '';
 }
 async function act(key: string, operation: () => Promise<void>) {
  if (busy) return;
  busy = key; error = ''; notice = '';
  try { await operation(); } catch (e) { if (current()) error = assistantError(e); }
  finally { if (current()) busy = ''; }
 }
 async function save(event: SubmitEvent) {
  event.preventDefault();
  if (!canSave) return;
  const target = agentChoice === 'dedicated' ? dedicatedAgent?.id : agentId;
  const created = agentChoice === 'dedicated' && !target;
  await act('save', async () => {
   const patch: AssistantPatch = {
    ...(created ? { create_agent: true } : { agent_id: target }),
    ...(created || assistant?.agent_id !== target ? { enabled: false, paused: true } : {}),
    ...(identityEditable ? { assistant_name: assistantName.trim() || DEFAULT_ASSISTANT_NAME, avatar_id: avatar } : {}),
    ...(activating ? { enabled: true, paused: false } : {})
   };
   const next = await configureAssistant(patch);
   if (!current()) return;
   acceptAssistant(next); editing = false;
   notice = activating ? 'Background research enabled. Only your opted-in sources can be observed.' : created ? 'Assistant created. Open the conversation whenever you’re ready.' : 'Assistant saved. Existing prompts, models and permissions are unchanged.';
   await appData.loadProfiles();
   await load();
  });
 }
 function conversation(voice = false) {
  void act(voice ? 'voice' : 'open', async () => {
   const home = await openAssistant();
   if (!current()) return;
   await appData.loadSessions();
   if (!current()) return;
   openSession(home.session_id);
   if (voice) await startCallForSession(home.session_id, { attach: true });
  });
 }
 function pause() { void act('pause', async () => { const next = await pauseAssistant(); if (current()) { acceptAssistant(next); notice = 'Background research paused. Your conversation is still available.'; await load(); } }); }
 function resume() { void act('resume', async () => { const next = await resumeAssistant(); if (current()) { acceptAssistant(next); notice = 'Background research resumed with your opted-in sources.'; await load(); } }); }
 function toggleSource(source: AssistantSource) {
  void act(source.id, async () => { const next = await setAssistantSource(source.connector_id, !source.enabled); if (current()) sources = sources.map(s => s.id === next.id ? next : s); });
 }
 const money = (n: number) => new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(n);
 const when = (value: string) => new Date(value).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
</script>

<div class="assistant-home">
 <div class="home-top"><span class="eyebrow">YOUR PERSONAL ASSISTANT</span><button class="icon-button" aria-label="Refresh assistant" title="Refresh assistant" disabled={refreshing || !!busy} onclick={() => load()}><RefreshCw size={16} class={refreshing ? 'spin' : ''} /></button></div>
 {#if error}<div class="message error" role="alert"><span>{error}</span><button onclick={() => load()} disabled={refreshing}>Retry refresh</button></div>{/if}
 {#if notice}<p class="message" role="status"><Check size={16} />{notice}</p>{/if}
 {#if loading}
  <div class="loading" role="status"><span class="skeleton orb"></span><span class="skeleton line"></span><span class="skeleton line short"></span><p>Getting your assistant ready…</p></div>
 {:else if assistant}
  <header class="hero">
   <div class="portrait"><AgentAvatar avatar={editing && identityEditable ? avatar : assistant.avatar_id} size={100} /><span class="portrait-orbit" aria-hidden="true"></span></div>
   <div class="hero-copy"><div class="status" class:enabled={assistant.status === 'ready'}><span aria-hidden="true"></span>{statusText}</div>
    <h1>{hasAgent ? assistant.assistant_name : 'A familiar face. A lasting conversation.'}</h1>
    <p>{hasAgent ? `${assistant.introduction.preferred_name ? `Hello, ${assistant.introduction.preferred_name}. ` : ''}Pick up where you left off. Your assistant can coordinate work across your sessions, with access and limits you control.` : 'Give it a name and a face. It gets one lasting conversation, and you can change any of this later.'}</p>
    {#if hasAgent && !editing}
     <div class="hero-actions"><button class="primary" disabled={!!busy} onclick={() => conversation()}><MessageSquare size={17} />{busy === 'open' ? 'Opening…' : 'Open conversation'}<ArrowUpRight size={16} /></button>
      {#if voiceEnabled}<button class="secondary" disabled={!!busy} onclick={() => conversation(true)}><AudioLines size={17} />{busy === 'voice' ? 'Connecting…' : 'Start call'}</button>{/if}
      <button class="icon-button" aria-label="Edit assistant" title="Edit assistant" disabled={!!busy} onclick={() => beginEdit()}><Settings2 size={18} /></button>
     </div>
     <p class="micro">Text and voice share this conversation. The microphone is only used when you start a call or dictate.</p>
    {/if}
   </div>
  </header>

  {#if editing}
   <form class="setup" onsubmit={save}>
    <fieldset disabled={!!busy}>
     <div class="section-heading"><div><span class="eyebrow">01 / IDENTITY</span><h2>{assistant.configured ? 'Make it yours' : 'Meet your assistant'}</h2></div></div>
     <label class="name-field">Assistant name<input required={identityEditable} disabled={!identityEditable} maxlength="80" bind:value={assistantName} placeholder={DEFAULT_ASSISTANT_NAME} /></label>
     <span class="field-label" id="avatar-label">Avatar</span>
     <div class="avatar-picker" role="group" aria-labelledby="avatar-label">{#each AVATARS as choice}<button type="button" class:selected={avatar === choice} aria-pressed={avatar === choice} aria-label={`${choice} avatar`} title={choice} disabled={!identityEditable} onclick={() => avatar = choice}><AgentAvatar avatar={choice} size={44} /></button>{/each}</div>

     <div class="section-heading"><div><span class="eyebrow">02 / AGENT</span><h2>What runs it</h2></div><button type="button" class="text-button" onclick={() => layout.openFeaturePage('profile')}><Plus size={15} />Manage agents</button></div>
     <div class="agent-choices" role="radiogroup" aria-label="Assistant agent kind">
      <label class="choice" class:selected={agentChoice === 'dedicated'}><input type="radio" name="agent-kind" value="dedicated" checked={agentChoice === 'dedicated'} onchange={() => chooseAgentKind('dedicated')} /><span><strong>A dedicated assistant agent</strong><small>{dedicatedAgent ? `Uses ${dedicatedAgent.name}, the agent setup made earlier.` : 'Setup creates a new agent on your default text model (Settings → Providers → Generated text).'} You can change its instructions and tools in Agents.</small></span></label>
      <label class="choice" class:selected={agentChoice === 'mine'}><input type="radio" name="agent-kind" value="mine" checked={agentChoice === 'mine'} onchange={() => chooseAgentKind('mine')} /><span><strong>One of my agents</strong><small>Keeps that agent’s prompts, models and permissions. Switching later moves your home conversation to the new agent.</small></span></label>
     </div>
     {#if agentChoice === 'mine'}
      <label>Assistant agent<select required bind:value={agentId} onchange={selectAgent}><option value="" disabled>Choose an existing agent</option>{#each $profiles as agent (agent.id)}<option value={agent.id}>{agent.name}{agent.is_builtin ? ' · built-in' : ''}</option>{/each}</select></label>
      {#if !$profiles.length}<p class="message">No agents yet. Use a dedicated assistant agent, or create one in Agents.</p>{/if}
      {#if chosenAgent?.is_builtin}<p class="micro">Built-in identity is protected. Create a custom agent in Agents to give it a new name and avatar.</p>{/if}
     {/if}

     {#if !assistant.enabled || assistant.paused}<label class="activation check-label"><input type="checkbox" bind:checked={activating} /><span><strong>Enable background research</strong><small>Lets your assistant look into opted-in sources while you’re away.</small></span></label>{/if}
     <div class="form-actions"><button type="submit" class="primary" disabled={!canSave || !!busy}>{busy === 'save' ? 'Saving…' : activating ? 'Save & enable research' : assistant.configured ? 'Save assistant' : 'Create assistant'}<Check size={16} /></button>{#if assistant.configured}<button type="button" class="secondary" onclick={() => editing = false}>Cancel</button>{/if}</div>
    </fieldset>
   </form>
  {:else if assistant.configured}
   <div class="overview">
    <section class="research-panel" aria-label="Background research status">
     <div class="section-heading"><div><span class="eyebrow">PRESENCE</span><h2>While you’re away</h2></div><ShieldCheck size={20} /></div>
     <p class="muted">{assistant.status === 'ready' ? `Watching ${optedIn} opted-in ${optedIn === 1 ? 'source' : 'sources'}. Background work is read-only.` : assistant.status === 'guard_unavailable' ? 'Research cannot run until the server’s read-only safety guard is available.' : 'Background research is off. You can still talk, plan and coordinate work in your conversation.'}</p>
     <div class="usage-grid"><div><strong>{assistant.usage.runs_today}</strong><small>runs · last 24 hours</small></div><div><strong>{assistant.usage.in_flight_runs}</strong><small>in flight</small></div><div><strong>{money(assistant.usage.reported_cost_today)}</strong><small>reported · last 24 hours</small></div></div>
     <p class="micro">Costs are reported after runs, not a hard billing cap.</p>
     <div class="panel-actions">
      {#if assistant.enabled && !assistant.paused}<button class="secondary" disabled={!!busy} onclick={pause}><Pause size={15} />{busy === 'pause' ? 'Pausing…' : 'Pause background'} </button>
      {:else if assistant.enabled && assistant.paused}<button class="secondary" disabled={!!busy || !hasAgent} onclick={resume}><Play size={15} />{busy === 'resume' ? 'Resuming…' : 'Resume background'}</button>
      {:else}<button class="secondary" disabled={!!busy || !hasAgent} onclick={() => beginEdit(true)}><Play size={15} />Review & enable</button>{/if}
      <button class="text-button" disabled={!!busy} onclick={() => beginEdit()}>Edit setup<ChevronRight size={14} /></button>
     </div>
    </section>
    <section class="identity-note"><AgentAvatar avatar={assistant.avatar_id} size={48} /><h2>Same agent.<br />More continuity.</h2><p class="muted">Your instructions, models, tools and permissions stay in Agents. Home adds a lasting conversation and optional read-only research with fixed limits.</p><button class="text-button" onclick={() => layout.openFeaturePage('profile', assistant.agent_id)}>Open agent settings<ArrowUpRight size={15} /></button></section>
   </div>
  {/if}

  <section class="sources section" aria-labelledby="sources-heading"><div class="section-heading"><div><span class="eyebrow">EXPLICIT ACCESS</span><h2 id="sources-heading">Opted-in sources <span class="count">{optedIn}</span></h2></div><button class="text-button" onclick={() => layout.openFeaturePage('plugins')}>Manage connections<ArrowUpRight size={14} /></button></div><p class="muted">Only authorized connections on this worker can be enabled. Opting in permits read-only background observation, never action on instructions found in a source.</p>
   {#if !sources.length}<div class="empty"><ShieldCheck size={24} /><div><strong>No sources connected</strong><p>Connect a service in Build → Plugins, then return here to opt in. Your assistant works without background sources.</p></div></div>
   {:else}<ul class="source-list">{#each sources as source (source.id)}<li><div class="source-icon"><ShieldCheck size={19} /></div><div class="source-copy"><strong>{source.name}</strong><p>{source.type} · {source.available ? 'Available on this worker' : 'Unavailable · check connection, grant and agent access'}</p>{#if source.last_observed_at}<small>Last observed {when(source.last_observed_at)}</small>{/if}{#if source.last_error}<p class="source-error">{source.last_error}</p>{/if}</div><button class="source-toggle" role="switch" aria-checked={source.enabled} aria-label={`Observe ${source.name}`} disabled={!!busy || (!source.available && !source.enabled) || !assistant.configured} onclick={() => toggleSource(source)}><span aria-hidden="true"></span><span>{busy === source.id ? 'Saving…' : source.enabled ? 'On' : 'Off'}</span></button></li>{/each}</ul>{/if}
  </section>
  <section class="section" aria-labelledby="activity-heading"><div class="section-heading"><div><span class="eyebrow">IN THE OPEN</span><h2 id="activity-heading">Activity & linked work</h2></div><Activity size={20} /></div>
   {#if !activity.length}<div class="empty"><Activity size={24} /><div><strong>Nothing running in the background</strong><p>Linked tasks and research will appear here with their source, outcome and reported cost.</p></div></div>
   {:else}<ol class="activity-list">{#each activity as item (item.id)}<li><AgentAvatar avatar={item.avatar_id} size={36} /><div class="activity-copy"><div class="activity-title">{#if item.session_id}<button class="text-button" onclick={() => item.session_id && openSession(item.session_id)}>{item.session_title || item.agent_name || 'Open linked conversation'}<ArrowUpRight size={14} /></button>{:else}<strong>{item.agent_name || 'Background research'}</strong>{/if}<span class="activity-status">{item.status}</span></div><p>{item.kind} · {when(item.created_at)} · {money(item.cost_usd)} reported</p>{#if item.source_ids.length}<small>Sources: {item.source_ids.map(id => sources.find(s => s.id === id)?.name ?? 'Unavailable source').join(', ')}</small>{/if}{#if item.error}<p class="source-error">{item.error}</p>{/if}</div></li>{/each}</ol>{/if}
  </section>
 {/if}
</div>

<style>
 .assistant-home { width:100%; max-width:1080px; margin:0 auto; padding:32px clamp(20px,4vw,56px) 64px; color:var(--foreground); }
 .home-top,.section-heading,.hero-actions,.panel-actions,.form-actions { display:flex; align-items:center; gap:12px; }
 .home-top,.section-heading { justify-content:space-between; }
 .eyebrow { font-family:var(--font-mono); font-size:10px; letter-spacing:.14em; color:var(--muted-foreground); }
 .hero { display:grid; grid-template-columns:120px minmax(0,1fr); gap:28px; padding:48px 0 40px; align-items:start; }
 .portrait { position:relative; display:grid; place-items:center; min-height:120px; }
 .portrait-orbit { position:absolute; width:128px; height:128px; border:1px dashed color-mix(in oklch,var(--primary) 24%,transparent); border-radius:50%; transform:rotate(-20deg); pointer-events:none; }
 h1 { font-family:var(--font-display); font-size:clamp(30px,4vw,46px); font-weight:500; line-height:1.16; letter-spacing:-.035em; margin:14px 0 18px; white-space:pre-line; overflow-wrap:anywhere; }
 .hero-copy>p { max-width:610px; font-size:14px; line-height:1.75; color:var(--muted-foreground); }
 .status { display:inline-flex; align-items:center; gap:7px; font-size:11px; font-weight:550; color:var(--muted-foreground); }
 .status>span { width:6px; height:6px; border-radius:50%; background:currentColor; }
 .status.enabled { color:var(--primary); }
 .hero-actions { flex-wrap:wrap; margin-top:22px; }
 button { cursor:pointer; transition:background .2s, border-color .2s, transform .2s; }
 button:active:not(:disabled) { transform:translateY(1px); }
 button:disabled,fieldset:disabled { opacity:.55; cursor:not-allowed; }
 button:focus-visible,input:focus-visible,select:focus-visible { outline:2px solid var(--ring); outline-offset:3px; }
 .primary,.secondary,.icon-button,.text-button { display:inline-flex; align-items:center; justify-content:center; gap:8px; min-height:44px; border-radius:8px; border:1px solid transparent; font-size:12px; font-weight:550; }
 .primary { background:var(--primary); color:var(--primary-foreground); padding:0 18px; }
 .primary:hover:not(:disabled) { filter:brightness(1.08); }
 .secondary { color:var(--foreground); background:var(--card); border-color:var(--border); padding:0 14px; }
 .secondary:hover:not(:disabled),.icon-button:hover:not(:disabled) { background:var(--accent); border-color:var(--primary); }
 .icon-button { width:44px; flex:none; background:transparent; color:var(--muted-foreground); }
 .text-button { color:var(--primary); background:transparent; padding:0 3px; text-align:left; }
 .text-button:hover { text-decoration:underline; text-underline-offset:4px; }
 .micro { font-size:11px!important; line-height:1.65!important; color:var(--muted-foreground); margin:12px 0 0; }
 .muted { font-size:12px; color:var(--muted-foreground); line-height:1.8; margin:10px 0 18px; }
 h2 { font-size:18px; font-weight:550; letter-spacing:-.025em; margin:7px 0 0; }
 .section-heading> :global(svg) { color:var(--primary); flex:none; }
 .overview { display:grid; grid-template-columns:minmax(0,1.6fr) minmax(0,1fr); border:1px solid var(--border); border-radius:14px; background:var(--card); overflow:hidden; }
 .research-panel { padding:26px; }
 .identity-note { padding:26px; border-left:1px solid var(--border); background:color-mix(in oklch,var(--primary) 3%,var(--background)); }
 .identity-note h2 { font-family:var(--font-display); font-size:25px; font-weight:400; margin-top:16px; }
 .usage-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:24px 0 18px; }
 .usage-grid strong { font-size:22px; letter-spacing:-.04em; font-weight:500; white-space:nowrap; }
 .usage-grid small { display:block; margin-top:5px; font-size:10px; line-height:1.6; color:var(--muted-foreground); }
 .panel-actions { margin-top:20px; flex-wrap:wrap; }
 .section { padding-top:34px; margin-top:30px; border-top:1px solid var(--border); }
 .count { display:inline-block; border:1px solid var(--border); color:var(--muted-foreground); border-radius:5px; padding:2px 6px; font-family:var(--font-mono); font-size:10px; vertical-align:middle; margin-left:6px; }
 .empty { display:flex; gap:16px; align-items:center; border:1px dashed var(--border); border-radius:10px; padding:24px; margin-top:20px; color:var(--muted-foreground); }
 .empty> :global(svg) { flex:none; color:var(--primary); }
 .empty strong { color:var(--foreground); font-size:12px; font-weight:500; }
 .empty p { font-size:12px; line-height:1.7; margin:6px 0 0; }
 ul,ol { list-style:none; padding:0; margin:20px 0 0; }
 li { display:flex; align-items:center; gap:14px; padding:18px 0; border-bottom:1px solid var(--border); }
 li:last-child { border-bottom:0; }
 .source-icon { color:var(--muted-foreground); }
 .source-copy,.activity-copy { flex:1; min-width:0; }
 li strong { font-size:13px; font-weight:500; overflow-wrap:anywhere; }
 li p,li small { display:block; font-size:11px; line-height:1.7; color:var(--muted-foreground); margin:3px 0; overflow-wrap:anywhere; }
 .source-toggle { display:inline-flex; align-items:center; gap:8px; min-height:44px; color:var(--muted-foreground); background:transparent; border:0; font-size:11px; flex:none; }
 .source-toggle>span:first-child { width:30px; height:18px; border:1px solid var(--border); border-radius:20px; background:var(--muted); position:relative; }
 .source-toggle>span:first-child::after { content:''; position:absolute; width:12px; height:12px; top:2px; left:2px; border-radius:50%; background:var(--muted-foreground); transition:transform .2s; }
 .source-toggle[aria-checked=true]>span:first-child { background:var(--primary); border-color:var(--primary); }
 .source-toggle[aria-checked=true]>span:first-child::after { transform:translateX(12px); background:var(--primary-foreground); }
 .source-toggle:hover { color:var(--primary); }
 .activity-title { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
 .activity-title .text-button { min-height:28px; overflow-wrap:anywhere; }
 .activity-status { font-family:var(--font-mono); font-size:10px; border:1px solid var(--border); padding:3px 7px; border-radius:5px; }
 .source-error { color:var(--destructive)!important; }
 .setup { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:28px; }
 fieldset { border:0; padding:0; margin:0; min-width:0; }
 .setup .section-heading:not(:first-child) { margin:32px 0 18px; padding-top:24px; border-top:1px solid var(--border); }
 label,.field-label { display:flex; flex-direction:column; gap:8px; font-size:12px; font-weight:500; margin:16px 0; }
 input,select { width:100%; border:1px solid var(--border); background:var(--background); color:var(--foreground); border-radius:7px; padding:11px 12px; font:inherit; min-height:44px; }
 input:hover,select:hover { border-color:var(--input-border-hover); }
 input:disabled { color:var(--muted-foreground); cursor:not-allowed; }
 .name-field { max-width:420px; }
 .avatar-picker { display:grid; grid-template-columns:repeat(4,max-content); gap:8px; margin:0 0 8px; }
 .avatar-picker button { display:flex; align-items:center; justify-content:center; border:1px solid transparent; border-radius:12px; padding:6px; background:transparent; }
 .agent-choices { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
 .choice { flex-direction:row; align-items:flex-start; gap:12px; margin:0; padding:16px; border:1px solid var(--border); border-radius:10px; background:var(--background); cursor:pointer; font-weight:400; }
 .choice.selected { border-color:var(--primary); background:color-mix(in oklch,var(--primary) 5%,var(--background)); }
 .choice input { width:18px; height:18px; min-height:0; padding:0; margin:1px 0 0; accent-color:var(--primary); flex:none; }
 .choice strong { display:block; font-size:12px; font-weight:550; }
 .choice small { display:block; margin-top:6px; color:var(--muted-foreground); font-size:11px; line-height:1.6; }
 .avatar-picker button.selected,.avatar-picker button:hover:not(:disabled) { border-color:var(--primary); background:color-mix(in oklch,var(--primary) 6%,transparent); color:var(--foreground); }
 .check-label { flex-direction:row; align-items:center; gap:12px; min-height:44px; font-weight:400; }
 .check-label input { width:18px; height:18px; min-height:0; padding:0; accent-color:var(--primary); flex:none; }
 .activation { padding:16px; border-radius:8px; background:var(--background); border:1px solid var(--border); margin-top:22px; }
 .activation strong { font-size:12px; font-weight:500; }
 .activation small { display:block; color:var(--muted-foreground); font-size:11px; margin-top:6px; line-height:1.6; }
 .form-actions { margin-top:24px; }
 .message { display:flex; align-items:center; gap:10px; padding:14px 16px; border:1px solid var(--border); background:var(--card); color:var(--foreground); border-radius:8px; font-size:12px; line-height:1.7; margin:14px 0; }
 .message :global(svg) { flex:none; color:var(--primary); }
 .message.error { border-color:var(--destructive); flex-wrap:wrap; }
 .message button { background:transparent; color:var(--primary); border:0; min-height:44px; text-decoration:underline; }
 .loading { padding:60px 0; text-align:center; color:var(--muted-foreground); font-size:12px; }
 .skeleton { display:block; background:var(--muted); border-radius:8px; margin:20px auto; animation:pulse 1.6s ease-in-out infinite alternate; }
 .orb { width:90px; height:90px; border-radius:30px; }.line { max-width:320px; height:26px; }.short { max-width:220px; height:12px; }
 :global(.spin) { animation:spin 1s linear infinite; }
 @keyframes spin { to { transform:rotate(360deg); } } @keyframes pulse { to { opacity:.4; } }
 @media(min-width:800px) { .hero { animation:arrive .35s ease-out; } @keyframes arrive { from { opacity:0; transform:translateY(8px); } } }
 @media(max-width:760px) { .assistant-home { padding-top:20px; } .hero { grid-template-columns:1fr; gap:14px; padding:24px 0 30px; } .portrait { justify-self:start; min-height:108px; margin-left:4px; } .portrait-orbit { width:118px; height:118px; } .hero-copy>p { font-size:13px; } h1 { font-size:34px; } .overview { grid-template-columns:1fr; } .identity-note { border-left:0; border-top:1px solid var(--border); } .identity-note h2 br { display:none; } .research-panel,.identity-note,.setup { padding:20px; } .agent-choices { grid-template-columns:1fr; } .section-heading { flex-wrap:wrap; gap:8px; } .section-heading .text-button { font-size:11px; } .section { margin-top:24px; padding-top:24px; } }
 @media(max-width:430px) { .avatar-picker { gap:4px; } .usage-grid { gap:8px; } .usage-grid strong { font-size:20px; } .usage-grid small { font-size:10px; } .empty { padding:18px; } }
 @media(prefers-reduced-motion:reduce) { .hero,.skeleton,:global(.spin) { animation:none!important; } button,.source-toggle>span:first-child::after { transition:none; } }
</style>