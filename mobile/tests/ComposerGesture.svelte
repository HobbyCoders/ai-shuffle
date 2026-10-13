<script lang="ts">
	import ChatInput from '../../frontend/src/lib/components/chat/ChatInput.svelte';
	// The app shell's banner host, so brief explanations (a tap on a blocked
	// call button) render as they do in the app.
	import BannerHost from '../../frontend/src/lib/components/BannerHost.svelte';
	let { compact = true }: { compact?: boolean } = $props();
</script>

<BannerHost />

<main class="chat-view" class:is-empty={!compact}>
	<header>
		<h1>Composer touch regression</h1>
		<p>{compact ? 'Existing conversation' : 'Centered new session'} · isolated transports · no messages sent</p>
	</header>
	<div class="input-slot"><ChatInput {compact} /></div>
</main>

<style>
	main { height: 100dvh; display: flex; flex-direction: column; overflow-y: auto; }
	header { padding: 24px 16px; }
	h1 { font-size: 18px; }
	p { font-size: 13px; }
	.input-slot { width: 100%; max-width: 760px; margin: auto auto 20px; flex-shrink: 0; }
	.is-empty header { margin-top: auto; }
	.is-empty .input-slot { margin-top: 12px; margin-bottom: auto; }
</style>
