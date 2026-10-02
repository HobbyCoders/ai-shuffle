<script lang="ts">
	import ChatAccess from '../src/lib/components/chat/ChatAccess.svelte';
	const sessionId = new URLSearchParams(location.search).get('session') || 'remote-chat';
</script>

<!-- A chat-header-sized strip: the access button sits where the header puts it. -->
<header class="strip">
	<span class="title">Admin chat</span>
	<ChatAccess {sessionId} />
</header>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.strip { display: flex; align-items: center; gap: 8px; padding: 8px 12px; box-sizing: border-box; border-bottom: 1px solid var(--border); }
	.title { flex: 1; font-size: 13px; font-weight: 600; }
</style>
