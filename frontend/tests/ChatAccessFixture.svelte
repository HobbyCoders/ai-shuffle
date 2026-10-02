<script lang="ts">
	import ChatAccess from '../src/lib/components/chat/ChatAccess.svelte';
</script>

<!-- The header button and dialog as the chat header renders them. -->
<header class="strip">
	<span class="brand">Chat</span>
	<ChatAccess sessionId="fixture-session" />
</header>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.strip { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--border); }
	.brand { flex: 1; font-size: 13px; font-weight: 600; }
</style>
