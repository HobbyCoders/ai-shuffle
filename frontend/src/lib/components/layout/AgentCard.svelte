<script lang="ts">
	/**
	 * AgentCard — the new-session screen's lead (#371 step 4, design 1
	 * "Agent-first"). The agent is who you are talking to, so the screen opens
	 * on it: avatar, name (the agent menu), the model it will run, and where it
	 * runs. "Talk to <agent>" starts a call through the existing voice entry
	 * (VoiceCallButton). Effort, Fast and Access stay in the composer dock
	 * below, unchanged.
	 *
	 * Colour: the agent's avatar colour is a theme token (avatar-colors.ts),
	 * used only for identity — the avatar, a faint wash and the call orb.
	 */
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, profiles, profilesLoaded, projects } from '$lib/stores/app-data.svelte';
	import { agentIdentities } from '$lib/stores/assistant';
	import { workerInstalls } from '$lib/stores/worker-presence';
	import { platform } from '$lib/stores/platform.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { getDefaultModel, getModelLabel } from '$lib/constants/models';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
	import { avatarColorToken } from '$lib/components/assistant/avatar-colors';
	import type { AvatarId } from '$lib/api/assistant';
	import ProfileSelector from '$lib/components/chat/ProfileSelector.svelte';
	import VoiceCallButton from '$lib/components/chat/VoiceCallButton.svelte';
	import { runsOnVisible } from '$lib/components/chat/session-scope';
	import RunsOnPicker from './RunsOnPicker.svelte';

	const profile = $derived($profiles.find((p) => p.id === $activeSession.profile) ?? null);
	const agentName = $derived(profile?.name ?? null);
	const avatar = $derived(($agentIdentities.find((a) => a.id === profile?.id)?.avatar_id ?? 'robot') as AvatarId);
	const builtin = $derived(!!profile?.is_builtin);
	const color = $derived(builtin ? null : (profile?.avatar_color ?? null));
	const agentTint = $derived(builtin ? 'var(--muted-foreground)' : (avatarColorToken(color) ?? 'var(--primary)'));

	const modelLabel = $derived(getModelLabel($activeSession.modelOverride || getDefaultModel()));
	const projectName = $derived(
		$projects.find((p) => p.id === $activeSession.project)?.name ??
			$clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name ??
			null
	);
	const capability = $derived(projectName ? `Works in ${projectName}` : 'Starts and steers sessions anywhere');

	const phone = $derived(platform.isMobile);
	const selfInstall = $derived(($workerInstalls ?? []).find((i) => i.is_self) ?? null);
	const showRunsOn = $derived(
		runsOnVisible({ phone, runsOnId: $activeSession.deviceIntent?.id ?? null, connected: selfInstall })
	);
	const noAgents = $derived($profilesLoaded && $profiles.length === 0);
</script>

<section class="agent-card" style:--agent={agentTint} aria-label="Your agent">
	<div class="ac-top">
		<div class="ac-avatar">
			<AgentAvatar {avatar} size={phone ? 52 : 64} {color} neutral={builtin} />
		</div>
		<div class="ac-id">
			<ProfileSelector variant="card" />
			<p class="ac-line">
				<span class="ac-model" title="Model — change it in the composer">{modelLabel}</span>
				<span class="ac-dot" aria-hidden="true">·</span>
				<span class="ac-cap">{capability}</span>
			</p>
		</div>
		<div class="ac-call">
			<VoiceCallButton variant="card" {agentName} />
		</div>
	</div>
	{#if showRunsOn || noAgents}
		<div class="ac-rule" aria-hidden="true"></div>
		<div class="ac-ctrls">
			{#if noAgents}
				<button type="button" class="no-agents" onclick={() => layout.openFeaturePage('profile')}>
					No agents yet — create one to start
				</button>
			{/if}
			{#if showRunsOn}
				<span class="grow" aria-hidden="true"></span>
				<RunsOnPicker />
			{/if}
		</div>
	{/if}
</section>

<style>
	.agent-card {
		position: relative;
		isolation: isolate;
		width: 100%;
		/* The composer's measure (ChatInput .chat-input-container), so the
		   card, the composer and the project row share both edges. */
		max-width: 48rem;
		margin: 0 auto;
		padding: 20px 20px 14px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 10px) * 1.6);
		background: var(--card);
		text-align: left;
		animation: card-rise 500ms cubic-bezier(0.16, 1, 0.3, 1) both;
	}
	/* Identity wash in the agent's colour, top-left only. */
	.agent-card::before {
		content: '';
		position: absolute;
		inset: 0;
		z-index: -1;
		border-radius: inherit;
		pointer-events: none;
		background: radial-gradient(90% 130% at 0% 0%, color-mix(in oklch, var(--agent) 13%, transparent), transparent 60%);
		transition: background 400ms ease;
	}
	.ac-top { display: flex; align-items: center; gap: 16px; }
	.ac-avatar { flex: none; display: grid; place-items: center; }
	.ac-avatar :global(.agent-avatar) { border-radius: 30%; }
	.ac-id { flex: 1; min-width: 0; }
	.ac-line {
		display: flex;
		align-items: center;
		gap: 6px;
		margin-top: 4px;
		min-width: 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
	}
	.ac-model { flex: none; color: var(--foreground); font-weight: 500; white-space: nowrap; }
	.ac-dot { color: var(--subtle-foreground); }
	.ac-cap { min-width: 0; color: var(--subtle-foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.ac-call { flex: none; }
	.ac-rule { height: 1px; margin: 16px -20px 12px; background: var(--border); }
	.ac-ctrls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
	.grow { flex: 1; }
	.no-agents {
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

	@keyframes card-rise { from { opacity: 0; transform: translateY(8px); } }

	@media (prefers-reduced-motion: reduce) {
		.agent-card { animation: none; }
		.agent-card::before { transition: none; }
	}
</style>
