<script lang="ts">
 /** Label over a field in a narrow navigation panel, without clipped row labels. */
 import type { Snippet } from 'svelte';
 let { label, children }: { label:string; children:Snippet } = $props();
</script>
<div class="panel-field"><span>{label}</span>{@render children()}</div>
<style>
 .panel-field { display:grid; gap:8px; padding:10px 12px; min-width:0; }
 span { font-size:12px; color:var(--st-mfg); }
</style>
