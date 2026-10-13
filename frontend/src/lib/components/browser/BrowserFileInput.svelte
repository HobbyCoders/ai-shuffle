<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { BrowserEditor } from '$lib/browser-panel/workspace';
  let { field, scale, left, top, onPick, onActive }: {
    field: BrowserEditor; scale: number; left: number; top: number;
    onPick: (field: BrowserEditor) => (files: File[]) => Promise<void>;
    onActive: (active: boolean) => void;
  } = $props();
  let deliver: ((files: File[]) => Promise<void>) | undefined;
  let busy = $state(false);
  let dismissed = $state(false);
  let active = false;
  function begin(e: Event) {
    e.stopPropagation();
    if (busy) return;
    active = true;
    onActive(true);
    deliver = onPick(field);
  }
  function finish() { active = false; onActive(false); }
  async function change(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    const send = deliver;
    input.value = ''; // allow choosing the same file again after a rejection
    if (!files.length || !send) { finish(); return; }
    busy = true;
    try { await send(files); } finally { busy = false; finish(); }
  }
  onDestroy(() => { if (active) finish(); });
</script>

<input
  class="native-editor file-picker"
  class:chooser={field.chooser}
  hidden={field.chooser && dismissed}
  type="file"
  aria-label={field.label || 'Choose files'}
  title={busy ? 'Uploading…' : 'Choose up to 10 files, 10 MB total'}
  accept={field.accept}
  multiple={field.multiple}
  disabled={busy}
  aria-busy={busy}
  style:left={`${left + field.x * scale}px`}
  style:top={`${top + field.y * scale}px`}
  style:width={`${field.width * scale}px`}
  style:height={`${Math.max(field.chooser ? 44 : 24, field.height * scale)}px`}
  onpointerdown={begin}
  onclick={(e) => { e.stopPropagation(); if (!active) begin(e); }}
  onkeydown={(e) => { e.stopPropagation(); if (e.key === 'Enter' || e.key === ' ') begin(e); }}
  onkeyup={(e) => e.stopPropagation()}
  oncontextmenu={(e) => e.stopPropagation()}
  onchange={change}
  oncancel={() => { dismissed = true; finish(); }}
/>
{#if busy}<span class="upload-status" role="status">Uploading files…</span>{/if}

<style>
  .file-picker { position: absolute; z-index: 4; min-width: 0; margin: 0; opacity: 0; cursor: pointer; touch-action: manipulation; }
  .file-picker.chooser { opacity: 1; background: white; color: black; border: 2px solid #6366f1; border-radius: 4px; font-size: 14px; }
  .file-picker:focus-visible { opacity: 1; background: white; color: black; outline: 2px solid #6366f1; }
  .upload-status { position: absolute; bottom: 8px; left: 8px; z-index: 5; padding: 6px 10px; border-radius: 6px; background: #18181b; color: white; font-size: 12px; pointer-events: none; }
</style>
