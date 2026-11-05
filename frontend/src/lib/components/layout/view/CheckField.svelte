<script lang="ts">
 import { Check } from 'lucide-svelte';
 let { checked = $bindable(false), label, disabled = false, onchange, describedby }: {
  checked?: boolean; label: string; disabled?: boolean; onchange?: (checked:boolean) => void; describedby?: string;
 } = $props();
</script>
<label class="check-field" class:disabled><input type="checkbox" bind:checked {disabled} aria-label={label} aria-describedby={describedby} onchange={() => onchange?.(checked)} /><span class="box" aria-hidden="true">{#if checked}<Check size={12} />{/if}</span><span>{label}</span></label>
<style>
 .check-field { position:relative; display:inline-flex; gap:8px; align-items:center; min-height:32px; font-size:13px; color:var(--foreground); cursor:pointer; }
 input { position:absolute; opacity:0; width:1px; height:1px; }
 .box { display:grid; place-items:center; width:16px; height:16px; flex:none; border:1px solid var(--field-border); border-radius:4px; background:var(--field-bg); }
 input:checked + .box { background:var(--action); color:var(--action-foreground); border-color:var(--action); }
 input:focus-visible + .box { outline:2px solid var(--ring); outline-offset:2px; }
 .check-field:hover:not(.disabled) .box { border-color:var(--foreground); }
 .disabled { opacity:.5; cursor:not-allowed; }
 @media (max-width:600px), (pointer:coarse) { .check-field { min-height:44px; } }
</style>
