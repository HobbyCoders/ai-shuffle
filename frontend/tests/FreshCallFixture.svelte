<script lang="ts">
	import ChatInput from '../src/lib/components/chat/ChatInput.svelte';
</script>

<main>
	<h1>New-session call</h1>
	<p>Isolated fixture · stubbed session create and call · microphone denied</p>
	<section class="composer"><ChatInput /></section>
</main>

<style>
	main {
		max-width: 900px;
		margin: auto;
		padding: 24px 12px;
	}

	h1 {
		font-size: 24px;
		font-weight: 600;
	}

	p {
		color: var(--muted-foreground);
		margin: 8px 0 32px;
		font-size: 13px;
	}
</style>
