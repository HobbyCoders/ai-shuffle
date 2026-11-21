<script lang="ts">
	import VaultPanel from '../src/lib/components/chat/VaultPanel.svelte';
	import VaultChip from '../src/lib/components/chat/VaultChip.svelte';
	const sessionId = new URLSearchParams(location.search).get('session') || 'remote-chat';
</script>

<!-- A chat-sized frame: the Vault sits with the composer the way ChatView
     mounts it. Waiting requests show inline above the composer, and the chip
     in the composer row opens the panel that lists active access. The header
     carries no Vault control. -->
<div class="frame">
	<header class="strip">
		<span class="title">Admin chat</span>
		<span class="neighbour" aria-hidden="true">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke-linecap="round" stroke-linejoin="round" /></svg>
		</span>
		<span class="git" aria-hidden="true">main</span>
	</header>
	<main class="transcript">
		<p class="bubble user">Check why the backup job on the NAS failed last night.</p>
		<p class="bubble">I need to read the job logs on the NAS. I’ve asked for temporary access below.</p>
	</main>
	<div class="slot">
		<VaultPanel {sessionId} />
		<div class="composer-wrap">
			<div class="composer">
				<span class="placeholder" aria-hidden="true">Message…</span>
				<div class="row"><VaultChip /></div>
			</div>
		</div>
	</div>
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
	.frame { --column-width: 48rem; }
	.slot { flex: none; }
	.composer-wrap { padding: 0 12px 12px; }
	.composer { max-width: var(--column-width); margin: 0 auto; box-sizing: border-box; padding: 12px 12px 6px 16px; border: 1px solid var(--border); border-radius: 16px; background: var(--card); color: var(--muted-foreground); font-size: 14px; }
	.row { display: flex; align-items: center; min-height: 28px; margin-top: 8px; }
	@media (max-width: 639px) { .neighbour { width: 44px; height: 44px; } }
</style>
