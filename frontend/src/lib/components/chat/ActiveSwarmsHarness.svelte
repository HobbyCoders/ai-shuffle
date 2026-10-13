<script lang="ts">
 import type { Writable } from 'svelte/store';
 import ActiveSwarms from './ActiveSwarms.svelte';
 let { session }: { session: Writable<{ sessionId: string; messages: string[] }> } = $props();
</script>

{#each $session.messages as message}<p>{message}</p>{/each}
<ActiveSwarms sessionId={$session.sessionId} />
