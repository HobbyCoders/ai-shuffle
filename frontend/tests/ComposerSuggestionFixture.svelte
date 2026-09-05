<script lang="ts">
	import ChatInput from '../src/lib/components/chat/ChatInput.svelte';
	import ChatBehaviorSection from '../src/lib/components/deck/cards/settings/ChatBehaviorSection.svelte';
	import { chatPreferences } from '../src/lib/stores/chatPreferences.svelte';
</script>

<main>
	<h1>Reply suggestions</h1>
	<p>Isolated fixture · mocked transports · no model calls</p>
	<section class="composer"><ChatInput /></section>
	<section class="settings"><ChatBehaviorSection chatPrefs={$chatPreferences} /></section>
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

	.composer {
		min-height: 180px;
	}

	.settings {
		padding: 12px;
	}

	:global(.set-group) {
		margin: 24px 0;
	}

	:global(.set-grouphead h3) {
		display: flex;
		align-items: center;
		gap: 8px;
		font-weight: 600;
	}

	:global(.set-grouphead svg) {
		width: 18px;
		height: 18px;
	}

	:global(.set-row) {
		display: flex;
		gap: 20px;
		justify-content: space-between;
		align-items: center;
		padding: 12px 0;
	}

	:global(.lab p),
	:global(.set-grouphead p) {
		color: var(--muted-foreground);
		font-size: 13px;
	}

	:global(.switch) {
		width: 36px;
		height: 22px;
		border-radius: 20px;
		background: var(--muted);
		display: flex;
		padding: 3px;
	}

	:global(.switch i) {
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--foreground);
	}

	:global(.switch.on) {
		background: var(--primary);
		justify-content: flex-end;
	}

	:global(button:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 3px;
	}
</style>
