<script lang="ts">
 import type { VoiceOptions, VoiceModelDetails } from '$lib/api/voice';
 let { options, details, saving, onSave }: {
  options: VoiceOptions; details: VoiceModelDetails;
  saving: boolean; onSave: (patch: VoiceOptions) => Promise<boolean>;
 } = $props();
 let draft = $state<VoiceOptions>({});
 let dirty = $state(false);
 const prompts = $derived(details.prompts.filter(p => p.key));
 function savedDraft() {
  const value: VoiceOptions = {opening: options.opening ?? ''};
  for (const prompt of prompts) value[prompt.key!] = options[prompt.key!] || prompt.default_text || prompt.text;
  return value;
 }
 $effect(() => { if (!dirty) draft = savedDraft(); });
 function change(key: 'instructions' | 'delegation_instructions' | 'opening', value: string) {
  draft = {...draft, [key]: value}; dirty = true;
 }
 async function submit(event: SubmitEvent) {
  event.preventDefault();
  const patch: VoiceOptions = {opening: draft.opening ?? ''};
  for (const prompt of prompts) {
   const value = draft[prompt.key!] ?? '';
   // An empty stored value follows future improvements to the factory prompt.
   patch[prompt.key!] = value === prompt.default_text ? '' : value;
  }
  if (await onSave(patch)) dirty = false;
 }
</script>

<form class="voice-options" onsubmit={submit} data-testid="voice-options">
 <h4>Conversation</h4>
 <label for="voice-opening">Opening words <span>(optional)</span></label>
 <input id="voice-opening" class="inp" maxlength="2000" value={draft.opening ?? ''} disabled={saving} placeholder="Wait for me to speak" oninput={e => change('opening', e.currentTarget.value)} />
 <p>Leave blank to listen first. New calls use these prompts; the chat composer controls the model, effort and permissions for tasks.</p>
 {#each prompts as prompt}
  <div class="prompt-heading">
   <label for={'voice-' + prompt.key}>{prompt.label}</label>
   <button type="button" class="btn" aria-label={`Restore default ${prompt.label}`} disabled={saving || draft[prompt.key!] === prompt.default_text} onclick={() => change(prompt.key!, prompt.default_text ?? '')}>Restore default<span class="sr-only"> {prompt.label}</span></button>
  </div>
  <textarea id={'voice-' + prompt.key} class="inp" rows="7" maxlength="16000" value={draft[prompt.key!] ?? ''} disabled={saving} oninput={e => change(prompt.key!, e.currentTarget.value)}></textarea>
 {/each}
 <p>{details.prompt_note}</p>
 <div class="actions">
  <button class="btn primary" type="submit" disabled={saving || !dirty}>{saving ? 'Saving…' : 'Save prompts'}</button>
  <button class="btn" type="button" disabled={saving || !dirty} onclick={() => { dirty = false; draft = savedDraft(); }}>Discard changes</button>
 </div>
</form>

<style>
 .voice-options { display: grid; gap: 10px; padding: 18px 0; border-top: 1px solid var(--border); min-width: 0; }
 h4 { margin: 0; font-size: 14px; }
 label { font-size: 13px; font-weight: 500; }
 p, label span { margin: 0; font-size: 12px; color: var(--text-3); line-height: 1.6; }
 .inp { width: 100%; box-sizing: border-box; }
 textarea { resize: vertical; min-height: 140px; line-height: 1.6; }
 .prompt-heading, .actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
 .prompt-heading { margin-top: 12px; }
 .actions { justify-content: flex-end; }
 .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
</style>
