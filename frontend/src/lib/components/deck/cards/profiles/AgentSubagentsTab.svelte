<script lang="ts">
	/**
	 * The Subagents tab: which agents this one may hand work to. "All agents",
	 * "Selected only" (the exact allowlist) or "None" (no subagents: the server
	 * takes Task and Swarm off this agent). Each caller's provider assignments
	 * still narrow the roster at run time.
	 */
	import type { AvatarId } from '$lib/api/assistant';
	import RowGroup from '$lib/components/settings/rows/RowGroup.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import Segmented from '$lib/components/settings/rows/Segmented.svelte';
	import Switch from '$lib/components/settings/rows/Switch.svelte';
	import InlineBanner from '$lib/components/settings/rows/InlineBanner.svelte';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
	import type { AgentInvokeMode, RosterAgent } from './agentForm';
	import { pinnedProviders } from './agentList';

	interface Props {
		/** Every other agent (never the one being edited). */
		agents: RosterAgent[];
		mode: AgentInvokeMode;
		enabled: string[];
		/** True when agents with no pinned model run on the subagent default. */
		hasDefault: boolean;
		avatarOf: (id: string) => AvatarId;
		colorOf: (id: string) => string | null;
		onMode: (mode: AgentInvokeMode) => void;
		onToggle: (id: string) => void;
		onSetEnabled: (ids: string[]) => void;
	}

	let { agents, mode, enabled, hasDefault, avatarOf, colorOf, onMode, onToggle, onSetEnabled }: Props = $props();

	const MODES: { value: AgentInvokeMode; label: string }[] = [
		{ value: 'all', label: 'All agents' },
		{ value: 'custom', label: 'Selected only' },
		{ value: 'none', label: 'None' }
	];

	const all = $derived(mode === 'all');
	const off = $derived(mode === 'none');
	const selected = $derived(enabled.filter((id) => agents.some((a) => a.id === id)).length);
	const summary = $derived(
		off ? 'None' : agents.length === 0 ? '' : all ? `All ${agents.length}` : `${selected} of ${agents.length}`
	);

	function reach(agent: RosterAgent): string {
		const models = { ...(agent.config?.provider_models ?? {}) };
		if (agent.config?.model) models[agent.config.provider || '__native__'] = agent.config.model;
		const n = pinnedProviders(models).length;
		if (n === 0) return hasDefault ? 'Subagent default' : 'Chat only';
		return `${n} provider${n === 1 ? '' : 's'}`;
	}
</script>

<RowGroup title="Roster">
	<SettingRow label="Who it can call" hint={off ? 'Task and Swarm are off.' : "Caller's provider still applies."}>
		<div class="roster-mode">
			<Segmented value={mode} options={MODES} onchange={(v) => onMode(v)} />
		</div>
	</SettingRow>
</RowGroup>

{#if mode === 'custom' && selected === 0 && agents.length > 0}
	<InlineBanner tone="info">Nothing selected. To turn subagents off, pick None.</InlineBanner>
{/if}

<RowGroup title="Agents" count={summary}>
	{#snippet actions()}
		{#if mode === 'custom' && agents.length > 0}
			<button type="button" class="lnk" onclick={() => onSetEnabled(agents.map((a) => a.id))}>Select all</button>
			<button type="button" class="lnk muted" onclick={() => onSetEnabled([])}>Clear</button>
		{/if}
	{/snippet}
	{#if off}
		<p class="none">This agent can't hand work to other agents.</p>
	{:else if agents.length === 0}
		<p class="none">No other agents yet.</p>
	{:else}
		{#each agents as agent (agent.id)}
			{@const on = all || enabled.includes(agent.id)}
			<div class="ra" class:dim={all}>
				<AgentAvatar avatar={avatarOf(agent.id)} size={28} color={colorOf(agent.id)} neutral={agent.is_builtin} />
				<div class="tx">
					<div class="nm">{agent.name}</div>
					<div class="ds">{reach(agent)}{agent.description ? ` · ${agent.description}` : ''}</div>
				</div>
				<Switch checked={on} label={`Allow ${agent.name}`} disabled={all} onchange={() => onToggle(agent.id)} />
			</div>
		{/each}
	{/if}
</RowGroup>

<style>
	.ra {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		gap: 12px;
		align-items: center;
		min-height: 54px;
		padding: 8px 16px;
	}
	.ra + .ra {
		border-top: 1px solid var(--st-border);
	}
	.tx {
		min-width: 0;
	}
	.nm {
		font-size: 14px;
		font-weight: 450;
	}
	.ds {
		font-size: 12.5px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.ra.dim .nm,
	.ra.dim .ds {
		opacity: 0.7;
	}
	.none {
		margin: 0;
		padding: 14px 16px;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.lnk {
		padding: 0;
		border: 0;
		background: none;
		color: var(--st-primary);
		font-weight: 600;
		font-size: 12.5px;
	}
	.lnk.muted {
		color: var(--st-mfg);
		font-weight: 500;
	}
	.lnk:hover {
		text-decoration: underline;
	}
	.roster-mode {
		max-width: 100%;
		min-width: 0;
	}

	/* Phone (the detail's narrow layout): the three choices share the row's
	   full width, and each one is a 44px touch target. The visible pill stays
	   38px; ::after lends the last few pixels without moving anything. */
	@container agd (max-width: 560px) {
		/* Outranks AgentDetail's narrow-row `.agd .st-row > .st-rc` sizing. */
		:global(.agd .st-row:has(.roster-mode) > .st-rc.st-rc) {
			flex: 1 1 100%;
		}
		.roster-mode {
			width: 100%;
		}
		.roster-mode :global(.st-seg) {
			display: flex;
			width: 100%;
			overflow: visible;
		}
		.roster-mode :global(.st-seg button) {
			position: relative;
			flex: 1 1 0;
			min-width: 0;
			height: 38px;
			padding: 0 6px;
		}
		.roster-mode :global(.st-seg button::after) {
			content: '';
			position: absolute;
			inset: -3px 0;
		}
	}
</style>
