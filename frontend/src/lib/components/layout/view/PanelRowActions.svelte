<script lang="ts">
 /** A shared list row with independent trailing controls; never nest buttons. */
 import type { Snippet } from 'svelte';
 let { children, actions }: { children: Snippet; actions: Snippet } = $props();
</script>
<div class="panel-row"><div class="row-main">{@render children()}</div><div class="row-actions">{@render actions()}</div></div>
<style>
 .panel-row { display:flex; align-items:center; gap:2px; }
 .row-main { flex:1; min-width:0; }
 .row-actions { display:flex; flex:none; align-items:center; }
</style>
