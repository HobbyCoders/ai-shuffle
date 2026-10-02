<script lang="ts">
	import ExecutionModeBar from '../src/lib/components/layout/ExecutionModeBar.svelte';
	import { activeSession } from '../src/lib/stores/active-session';
	let status = $state('Ready');
	async function send() {
		status = 'Creating…';
		await activeSession.createWorktree();
		status = activeSession.current.sessionId ? `Created ${activeSession.current.sessionId}` : 'Create failed';
	}
	function newSession() {
		activeSession.newChat();
		status = 'New session';
	}
</script>
<header>
	<h1>One-time branch choice</h1>
	<button onclick={send}>Send</button>
	<button onclick={newSession}>New session</button>
	<p role="status">{status}</p>
</header>
<main><ExecutionModeBar /></main>
<style>
	header { padding: 16px; border-bottom: 1px solid var(--border); }
	h1 { font-size: 18px; margin-bottom: 12px; }
	button { padding: 8px 12px; margin: 0 8px 8px 0; border: 1px solid var(--border); border-radius: 6px; }
	main { padding: 200px 16px 16px; }
</style>
