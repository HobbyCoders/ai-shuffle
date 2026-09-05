<script lang="ts">
 import ChatInput from '../src/lib/components/chat/ChatInput.svelte';
</script>
<main><h1>Composer touch regression</h1><p>Isolated composer. No messages are sent.</p><div class="composer"><ChatInput compact /></div></main>
<style>main { min-height: 100dvh; padding: 24px; } h1 { font-size: 18px; } p { font-size: 13px; } .composer { position: fixed; bottom: 20px; left: 0; right: 0; max-width: 760px; margin: auto; }</style>
