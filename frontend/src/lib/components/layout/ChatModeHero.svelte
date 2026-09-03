<script lang="ts">
	/**
	 * ChatModeHero — the new-session screen for the CHAT lane.
	 *
	 * The code lane's hero (WorkspaceSelector) is built around picking a
	 * workspace, because a code session is meaningless without a repository. A
	 * chat session is deliberately projectless — no repo, no worktree, and a tool
	 * ceiling of web search, web fetch, and a Read pinned to the files the user
	 * attaches — so there is nothing to pick here and a picker would only imply a
	 * binding that does not exist.
	 *
	 * What is left is the honest part: say what this lane can and cannot do
	 * BEFORE the user types, rather than letting them discover mid-conversation
	 * where its reach ends. The subline has to track the ceiling: it may not
	 * promise "nothing touches your machine" while attachments are readable, and
	 * it may not omit attachments now that the composer offers them. Same visual
	 * family as WorkspaceSelector (centered column, display-face heading, muted
	 * subline) so switching lanes reads as the same app, not a different product.
	 */
	import { MessagesSquare } from 'lucide-svelte';
</script>

<div class="chat-mode-hero">
	<div class="hero-mark" aria-hidden="true">
		<MessagesSquare size={22} strokeWidth={1.5} />
	</div>

	<h1 class="hero-heading">Let's talk</h1>

	<p class="hero-subline">
		A plain conversation, with web search and fetch. No repository and no worktree — the only files
		it can open are the ones you attach.
	</p>
</div>

<style>
	/* Mirrors .workspace-selector's metrics so the two heroes sit at the same
	   optical position when the lane switches. */
	.chat-mode-hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 2rem 2rem 1.25rem;
		text-align: center;
		user-select: none;
	}

	/* Same badge treatment as ChatView's cleared-session hero. */
	.hero-mark {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		margin-bottom: 10px;
		border-radius: 50%;
		color: var(--subtle-foreground);
		background: var(--scrim);
		border: 1px solid var(--border);
	}

	.hero-heading {
		font-family: var(--font-display, Georgia, serif);
		font-size: clamp(1.375rem, 4vw, 2rem);
		font-weight: 700;
		color: var(--foreground);
		letter-spacing: -0.02em;
		line-height: 1.15;
	}

	.hero-subline {
		font-size: 0.875rem;
		color: var(--muted-foreground);
		line-height: 1.5;
		max-width: 42ch;
	}
</style>
