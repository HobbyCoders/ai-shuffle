<script lang="ts">
	// The desktop composer (ChatInput → ControlsDock → ContextUsageBar) on an
	// xAI/OpenRouter session, for the context-meter check of #2274.
	import ChatInput from '../src/lib/components/chat/ChatInput.svelte';
	let { label }: { label: string } = $props();
</script>

<main class="chat-view">
	<p class="note">{label}</p>
	<div class="composer"><ChatInput /></div>
</main>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: var(--font-sans, Inter, system-ui, sans-serif); }
	.chat-view { position: relative; height: 100dvh; display: flex; flex-direction: column; justify-content: flex-end; max-width: 860px; margin: 0 auto; padding: 0 16px 24px; box-sizing: border-box; }
	.note { margin: 0 0 auto; padding-top: 16px; font-size: 13px; color: var(--muted-foreground); }
</style>
