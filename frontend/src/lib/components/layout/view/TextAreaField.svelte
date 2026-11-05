<script lang="ts">
 /** Multiline companion to TextField; explicit-save forms use oninput. */
 import { controlAria, getRowContext } from '../../settings/rows/row-context';
 import { getSettingsAccess } from '$lib/settings/access.svelte';
 let { value = '', oninput, label, placeholder, rows = 5 }: {
  value: string; oninput: (value: string) => void; label?: string; placeholder?: string; rows?: number;
 } = $props();
 const row = getRowContext();
 const access = getSettingsAccess();
</script>
<textarea {value} {placeholder} {rows} {...controlAria(row, label)} disabled={access.readOnly}
 oninput={(event) => oninput(event.currentTarget.value)}></textarea>
<style>
 textarea { display: block; width: 100%; min-width: 0; padding: 10px 12px; border: 1px solid var(--st-input); background: var(--st-field); border-radius: var(--st-radius-md); font: inherit; font-size: 13px; line-height: 1.55; color: var(--st-fg); resize: vertical; }
 textarea::placeholder { color: var(--st-subtle); }
 textarea:focus-visible { border-color: var(--st-field-focus); outline: 2px solid var(--st-primary); outline-offset: 2px; }
 textarea:disabled { opacity: .5; }
 @media (max-width: 600px) { textarea { font-size: 16px; } }
</style>
