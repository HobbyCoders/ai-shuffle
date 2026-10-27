<script lang="ts">
	/**
	 * NewSessionHero — the new-session screen's lead: the agent you are about
	 * to talk to, centred, then one sentence saying where it will run.
	 *
	 *   (avatar)
	 *   Overseer ⌄                    ← the agent menu (ProfileSelector `card`)
	 *   Runs on Unraid in ai-shuffle on a new branch from main in a worktree.
	 *
	 * Every part of the sentence opens the menu its composer chip opens
	 * (NewSessionSentence). The composer below carries no chip row while this
	 * shows; the row returns with the first message. Talk lives in the
	 * composer (the Talk circle that turns into Send).
	 *
	 * Colour: the agent's avatar colour is a theme token (avatar-colors.ts),
	 * used only for identity: the avatar and a faint halo behind it.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { profiles, profilesLoaded } from '$lib/stores/app-data.svelte';
	import { agentIdentities } from '$lib/stores/agent-identities';
	import { platform } from '$lib/stores/platform.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import AgentAvatar from '$lib/components/agents/AgentAvatar.svelte';
	import { avatarColorToken } from '$lib/components/agents/avatar-colors';
	import type { AvatarId } from '$lib/api/agent-identity';
	import ProfileSelector from '$lib/components/chat/ProfileSelector.svelte';
	import NewSessionSentence from './NewSessionSentence.svelte';

	const profile = $derived($profiles.find((p) => p.id === $activeSession.profile) ?? null);
	const avatar = $derived(($agentIdentities.find((a) => a.id === profile?.id)?.avatar_id ?? 'robot') as AvatarId);
	const builtin = $derived(!!profile?.is_builtin);
	const color = $derived(builtin ? null : (profile?.avatar_color ?? null));
	const agentTint = $derived(builtin ? 'var(--muted-foreground)' : (avatarColorToken(color) ?? 'var(--primary)'));
	const noAgents = $derived($profilesLoaded && $profiles.length === 0);
</script>

<section class="ns-hero" style:--agent={agentTint} aria-label="New session">
	<div class="ns-avatar">
		<AgentAvatar {avatar} size={platform.isMobile ? 64 : 72} {color} neutral={builtin} />
	</div>
	<ProfileSelector variant="card" />
	{#if noAgents}
		<button type="button" class="no-agents" onclick={() => layout.openFeaturePage('profile')}>
			No agents yet — create one to start
		</button>
	{/if}
	<NewSessionSentence />
</section>

<style>
	.ns-hero {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		width: 100%;
		max-width: 48rem;
		margin: 0 auto;
		padding: 0 0 28px;
		text-align: center;
		/* No transform-based animation here: a filled transform would make the
		   hero a stacking context, and its menus are moved to <body> anyway. */
		animation: ns-hero-in 450ms cubic-bezier(0.16, 1, 0.3, 1) both;
	}
	/* The avatar sits in a hairline ring with a faint halo in the agent's colour. */
	.ns-avatar {
		position: relative;
		isolation: isolate;
		display: grid;
		place-items: center;
		margin-bottom: 12px;
		padding: 4px;
		border-radius: 50%;
		box-shadow: inset 0 0 0 1px var(--border-strong);
	}
	.ns-avatar::before {
		content: '';
		position: absolute;
		inset: -26px;
		z-index: -1;
		border-radius: 50%;
		background: radial-gradient(closest-side, color-mix(in oklch, var(--agent) 16%, transparent), transparent);
		pointer-events: none;
		transition: background 400ms ease;
	}
	.ns-hero :global(.ns-sentence) { margin-top: 10px; }
	.no-agents {
		margin-top: 6px;
		padding: 0;
		border: none;
		background: none;
		color: var(--primary);
		font: inherit;
		font-size: 13px;
		cursor: pointer;
	}
	.no-agents:hover { text-decoration: underline; }
	.no-agents:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 4px; }

	@keyframes ns-hero-in { from { opacity: 0; } }

	@media (prefers-reduced-motion: reduce) {
		.ns-hero { animation: none; }
		.ns-avatar::before { transition: none; }
	}
</style>
