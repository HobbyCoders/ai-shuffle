<script lang="ts">
	import ChatAccess from '../src/lib/components/chat/ChatAccess.svelte';
	import PrivateAccessPrompt from '../src/lib/components/chat/PrivateAccessPrompt.svelte';
	const sessionId = new URLSearchParams(location.search).get('session') || 'remote-chat';
</script>

<!-- A chat-sized frame: the access control sits in the header where the app
     puts it (beside the other header icons), and waiting requests show above
     the composer the way ChatView docks PrivateAccessPrompt. -->
<div class="frame">
	<header class="strip">
		<span class="title">Admin chat</span>
		<span class="neighbour" aria-hidden="true">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke-linecap="round" stroke-linejoin="round" /></svg>
		</span>
		<ChatAccess {sessionId} />
		<span class="git" aria-hidden="true">main</span>
	</header>
	<main class="transcript">
		<p class="bubble user">Check why the backup job on the NAS failed last night.</p>
		<p class="bubble">I need to read the job logs on the NAS. I’ve asked for temporary access below.</p>
	</main>
	<PrivateAccessPrompt {sessionId} />
	<div class="composer" aria-hidden="true">Message…</div>
</div>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.frame { display: flex; flex-direction: column; height: 100dvh; }
	.strip { position: relative; display: flex; align-items: center; gap: 6px; padding: 8px 12px; box-sizing: border-box; border-bottom: 1px solid var(--border); }
	.title { flex: 1; font-size: 13px; font-weight: 600; }
	.neighbour { display: grid; place-items: center; width: 28px; height: 28px; color: var(--muted-foreground); }
	.neighbour svg { width: 16px; height: 16px; }
	.git { display: flex; align-items: center; height: 28px; padding: 0 8px; border: 1px solid var(--border); border-radius: calc(var(--radius, .75rem) * .7); font-size: 11px; color: var(--muted-foreground); }
	.transcript { flex: 1; overflow: auto; display: grid; align-content: start; gap: 12px; padding: 16px; max-width: 48rem; width: 100%; margin: 0 auto; box-sizing: border-box; }
	.bubble { margin: 0; font-size: 14px; line-height: 1.5; }
	.bubble.user { justify-self: end; padding: 8px 12px; border-radius: 12px; background: var(--muted); }
	.composer { margin: 0 12px 12px; padding: 14px 16px; border: 1px solid var(--border); border-radius: 16px; background: var(--card); color: var(--muted-foreground); font-size: 14px; }
	@media (max-width: 639px) { .neighbour { width: 44px; height: 44px; } }
</style>
