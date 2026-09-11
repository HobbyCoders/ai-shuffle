<script lang="ts">
 import type { VoiceOptions, VoiceModelDetails } from '$lib/api/voice';
 import { getModels, getProviders, encodeProviderModel } from '$lib/constants/models.svelte';
 import { resolveModelEffort } from '$lib/constants/model-effort';
 let { model, providerId, options, details, saving, onSave } : {
  model: string; providerId: string | null; options: VoiceOptions; details: VoiceModelDetails;
  saving: boolean; onSave: (patch: VoiceOptions) => Promise<boolean>;
 } = $props();
 let draft = $state<VoiceOptions>({});
 let dirty = $state(false);
 let error = $state('');
 $effect(() => { if (!dirty) draft = {...options}; });
 const live = $derived(model === 'gpt-live-1');
 const realtime = $derived(model.startsWith('gpt-realtime'));
 const responses = $derived(draft.delegation_type === 'responses');
 const providers = $derived(getProviders().filter(p => !responses || (p.provider_type === 'openai' && p.auth_kind === 'api' && (!providerId || p.id === providerId))));
 const capability = $derived(resolveModelEffort(draft.delegation_model));
 const efforts = $derived(draft.delegation_model ? capability.options.filter(e => !responses || ['none','minimal','low','medium','high','xhigh'].includes(e.value)) : []);
 const selectedAvailable = $derived(!draft.delegation_model || (!responses && getModels().some(m => m.value === draft.delegation_model)) || providers.some(p => p.models.some(m => encodeProviderModel(p.id,m.id) === draft.delegation_model)));
 function change<K extends keyof VoiceOptions>(key: K, value: VoiceOptions[K]) {
  draft = {...draft,[key]:value}; dirty = true; error = '';
 }
 function number(key: keyof VoiceOptions, raw: string) {
  change(key, raw.trim() === '' ? undefined : Number(raw));
 }
 function reset() { dirty = false; draft = {...options}; error = ''; }
 async function submit(event: SubmitEvent) {
  event.preventDefault();
  if (!selectedAvailable || (responses && !draft.delegation_model)) {error = 'Choose an available delegated model for this delegation type.'; return;}
  // Empty numeric fields must never silently reset a saved value.
  if (realtime && (typeof draft.max_output_tokens !== 'number' && draft.max_output_tokens !== 'inf')) {error = 'Enter a token limit or choose Unlimited.'; return;}
  if (realtime && draft.turn_detection === 'server_vad' && ['threshold','prefix_padding_ms','silence_duration_ms'].some(k => !Number.isFinite(draft[k as keyof VoiceOptions]))) {error='Enter all turn-detection values.';return;}
  const patch = Object.fromEntries(Object.entries(draft).filter(([k,v]) => v !== options[k as keyof VoiceOptions])) as VoiceOptions;
  if (await onSave(patch)) dirty = false;
 }
</script>

<form class="voice-options" onsubmit={submit} data-testid="voice-model-options">
 <h4>Model settings</h4>
 <p>Saved separately for each voice model. Applies to new calls; existing calls keep their saved settings.</p>
 {#if realtime}
  <label>Transcription model
   <select class="sel" aria-label="Transcription model" value={draft.transcription_model} disabled={saving} onchange={e => change('transcription_model',e.currentTarget.value)}>
    {#each details.transcription_models ?? [] as name}<option value={name}>{name}</option>{/each}
   </select>
  </label>
  <div class="field">
   <label class="check"><input type="checkbox" checked={draft.max_output_tokens === 'inf'} disabled={saving} onchange={e => change('max_output_tokens',e.currentTarget.checked ? 'inf' : 1024)} />Unlimited output tokens</label>
   {#if draft.max_output_tokens !== 'inf'}
    <label>Max output tokens<input class="inp" aria-label="Max output tokens" type="number" min="1" max="4096" step="1" required value={draft.max_output_tokens} disabled={saving} oninput={e => number('max_output_tokens',e.currentTarget.value)} /></label>
   {:else}<p>Max output tokens: inf (provider limit).</p>{/if}
  </div>
  <label>Turn detection
   <select class="sel" aria-label="Turn detection" value={draft.turn_detection} disabled={saving} onchange={e => change('turn_detection',e.currentTarget.value as VoiceOptions['turn_detection'])}>
    <option value="server_vad">Normal (server VAD)</option><option value="semantic_vad">Semantic VAD</option>
   </select>
  </label>
  {#if draft.turn_detection === 'server_vad'}
   <div class="tuning">
    <label>Threshold <output>{draft.threshold}</output><input type="range" aria-label="Threshold" min="0" max="1" step="0.01" value={draft.threshold} disabled={saving} oninput={e => number('threshold',e.currentTarget.value)} /></label>
    <label>Prefix padding (ms)<input class="inp" aria-label="Prefix padding (ms)" type="number" min="0" max="5000" step="1" required value={draft.prefix_padding_ms} disabled={saving} oninput={e => number('prefix_padding_ms',e.currentTarget.value)} /></label>
    <label>Silence duration (ms)<input class="inp" aria-label="Silence duration (ms)" type="number" min="100" max="5000" step="1" required value={draft.silence_duration_ms} disabled={saving} oninput={e => number('silence_duration_ms',e.currentTarget.value)} /></label>
    <label class="check"><input type="checkbox" aria-label="Enable idle timeout" checked={draft.idle_timeout_ms != null} disabled={saving} onchange={e => change('idle_timeout_ms',e.currentTarget.checked ? 6000 : null)} />Enable idle timeout</label>
    {#if draft.idle_timeout_ms != null}<label>Idle timeout (ms)<input class="inp" aria-label="Idle timeout (ms)" type="number" min="5000" max="30000" step="1" required value={draft.idle_timeout_ms} disabled={saving} oninput={e => number('idle_timeout_ms',e.currentTarget.value)} /></label>{/if}
   </div>
  {:else}
   <label>Eagerness<select class="sel" aria-label="Eagerness" value={draft.eagerness} disabled={saving} onchange={e => change('eagerness',e.currentTarget.value as VoiceOptions['eagerness'])}>{#each ['auto','low','medium','high'] as level}<option value={level}>{level}</option>{/each}</select></label>
   <p>Threshold, padding, silence duration and idle timeout apply only to Normal VAD. Their saved values are kept when switching modes.</p>
  {/if}
 {/if}
 {#if live}
  <label>Opening words<textarea class="inp" aria-label="Opening words" rows="2" maxlength="2000" value={draft.opening ?? ''} disabled={saving} placeholder="Leave empty to wait for the caller" oninput={e => change('opening',e.currentTarget.value)}></textarea></label>
  <p>Spoken at the beginning of a new browser call, not when reconnecting or opening a phone-mode call.</p>
 {/if}
 <label>Voice agent prompt<textarea class="inp" aria-label="Voice agent prompt" rows="5" maxlength="16000" value={draft.instructions ?? ''} disabled={saving} placeholder="Leave empty to use the system prompt shown below" oninput={e => change('instructions',e.currentTarget.value)}></textarea></label>
 <p>This replaces the base voice prompt. Required tool and device instructions are still appended where applicable.</p>
 {#if live}
  <h4>Delegation</h4>
  <label>Type<select class="sel" aria-label="Delegation type" value={draft.delegation_type} disabled={saving} onchange={e => {change('delegation_type',e.currentTarget.value as VoiceOptions['delegation_type']);change('delegation_model','');change('delegation_effort','');}}><option value="client">Client — AI Shuffle agent</option><option value="responses">Responses — OpenAI managed</option></select></label>
  <p>{responses ? 'Runs on the voice provider’s OpenAI account. AI Shuffle app tools, agents and local actions are not connected to this mode. The call composer cannot change this backend.' : 'Uses AI Shuffle’s agent permissions and tools. This is the default delegated model; a model explicitly chosen in the call composer takes precedence.'}</p>
  <label>Delegated model<select class="sel" aria-label="Delegated model" value={draft.delegation_model ?? ''} disabled={saving} required={responses} onchange={e => {change('delegation_model',e.currentTarget.value);change('delegation_effort','');}}>
   <option value="">{responses ? 'Choose an OpenAI model' : 'Use the call composer model'}</option>
   {#if !selectedAvailable}<option value={draft.delegation_model} disabled>{draft.delegation_model} (unavailable)</option>{/if}
   {#if !responses}<optgroup label="Native agents">{#each getModels() as m}<option value={m.value}>{m.label}</option>{/each}</optgroup>{/if}
   {#each providers as p}<optgroup label={p.name}>{#each p.models as m}<option value={encodeProviderModel(p.id,m.id)}>{m.label}</option>{/each}</optgroup>{/each}
  </select></label>
  <label>Reasoning effort<select class="sel" aria-label="Reasoning effort" value={draft.delegation_effort ?? ''} disabled={saving || !draft.delegation_model} onchange={e => change('delegation_effort',e.currentTarget.value)}><option value="">Model default / inherit</option>{#if draft.delegation_effort && !efforts.some(e => e.value === draft.delegation_effort)}<option value={draft.delegation_effort} disabled>{draft.delegation_effort} (no longer declared)</option>{/if}{#each efforts as effort}<option value={effort.value}>{effort.label}</option>{/each}</select></label>
  {#if draft.delegation_model && capability.explanation}<p>{capability.explanation}</p>{/if}
  <label>Delegated agent prompt<textarea class="inp" aria-label="Delegated agent prompt" rows="5" maxlength="16000" value={draft.delegation_instructions ?? ''} disabled={saving} placeholder="Leave empty to use the delegated-agent prompt shown below" oninput={e => change('delegation_instructions',e.currentTarget.value)}></textarea></label>
 {/if}
 {#if error}<p class="error" role="alert">{error}</p>{/if}
 <div class="actions"><button type="submit" disabled={saving || !dirty}>{saving ? 'Saving…' : 'Save model settings'}</button><button type="button" disabled={saving || !dirty} onclick={reset}>Cancel changes</button>{#if dirty}<span>Unsaved changes</span>{/if}</div>
</form>
<style>
 .voice-options {padding:16px;border-top:1px solid var(--border);display:grid;gap:14px;min-width:0}
 h4 {margin:0;font-size:14px} p {margin:0;color:var(--muted-foreground);font-size:12px;line-height:1.6}
 label,.field {display:grid;gap:7px;min-width:0;font-size:13px} .check {display:flex;align-items:center;gap:9px}
 .sel,.inp {width:100%;min-width:0;max-width:100%;box-sizing:border-box;background:var(--background);color:var(--foreground);border:1px solid var(--border);border-radius:7px;padding:9px;font:inherit}
 textarea {resize:vertical;min-height:65px} input[type=range] {width:100%;accent-color:var(--primary)} output {font-variant-numeric:tabular-nums}
 .tuning {display:grid;gap:14px;padding:12px;border:1px solid var(--border);border-radius:8px}
 .actions {display:flex;flex-wrap:wrap;align-items:center;gap:9px}.actions span {font-size:12px;color:var(--muted-foreground)}
 button {border:1px solid var(--border);border-radius:7px;padding:8px 12px;background:var(--background);color:var(--foreground);cursor:pointer}
 button[type=submit] {background:var(--primary);color:var(--primary-foreground)}button:disabled {opacity:.5;cursor:default}.error{color:var(--destructive)}
</style>
