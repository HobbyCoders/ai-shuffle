<script lang="ts">
	// The production AppTopBar (worker chip, trail, chat-actions chevron) with
	// the same actions MainPanel hands the phone shell: the real ChatAccess
	// and the compact GitHeaderActions. one-bar.ts answers every request.
	import AppTopBar from '../src/shell/AppTopBar.svelte';
	import ChatAccess from '../../frontend/src/lib/components/chat/ChatAccess.svelte';
	import GitHeaderActions from '../../frontend/src/lib/components/git/GitHeaderActions.svelte';

	let { sessionId, chat, subagent, gitRepo }: { sessionId: string; chat: boolean; subagent: boolean; gitRepo: boolean } = $props();
	const noop = () => {};
</script>

{#snippet actions()}
	<ChatAccess {sessionId} />
	<div class="git-actions-host">
		<GitHeaderActions
			{sessionId}
			isGitRepo={gitRepo}
			projectName="ai-shuffle"
			compact
			onClickCommit={noop}
			onClickCreatePR={noop}
			onClickPush={noop}
			onClickPull={noop}
			onClickInitRepo={noop}
			onClickShipIt={noop}
			onOpenDiffReview={noop}
		/>
	</div>
{/snippet}

<div class="app-shell">
	<AppTopBar
		onNewSession={noop}
		onSettledOpenSession={noop}
		onOpenSession={noop}
		onOpenFeature={noop}
		hideGlobalStt={chat}
		actions={chat && !subagent ? actions : undefined}
	/>
	<main class="transcript">
		{#if chat}
			<p class="msg-user">The Android worker bar flickers when I open the drawer on DeX. Can you find out why?</p>
			<p class="msg-text">I'll check how the page tells the app it draws its own worker bar, then look at the drawer's history entry.</p>
			<div class="tool-row"><b>Read</b><code>mobile/src/shell/native-shell.ts</code></div>
			<div class="tool-row"><b>Grep</b><code>"worker-bar" in android/</code></div>
		{:else}
			<p class="msg-text">Settings</p>
		{/if}
	</main>
	<div class="composer" aria-hidden="true">Message…</div>
</div>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: var(--font-sans, 'Inter Variable', system-ui, sans-serif); }
	.app-shell { display: flex; flex-direction: column; height: 100dvh; overflow: hidden; }
	.git-actions-host { position: relative; display: flex; align-items: center; }
	.transcript { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 16px 10px; display: flex; flex-direction: column; gap: 12px; }
	.msg-user { align-self: flex-end; max-width: 84%; margin: 0; padding: 10px 13px; border-radius: 16px; background: var(--muted); font-size: 14px; line-height: 1.5; }
	.msg-text { margin: 0; font-size: 14px; line-height: 1.55; }
	.tool-row { display: flex; gap: 8px; font-size: 12.5px; color: var(--muted-foreground); }
	.tool-row b { color: var(--foreground); font-weight: 550; }
	.tool-row code { font-family: var(--font-mono); font-size: 12px; color: var(--foreground); }
	.composer { margin: 0 12px 12px; padding: 14px 16px; border: 1px solid var(--border); border-radius: 16px; background: var(--card); color: var(--muted-foreground); font-size: 14px; }
</style>
