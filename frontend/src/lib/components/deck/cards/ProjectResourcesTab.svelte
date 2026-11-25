<script lang="ts" module>
	export type ResourceId = 'mcp' | 'agents' | 'hooks' | 'memory';
</script>

<script lang="ts">
	/**
	 * The project's resources on this worker: MCP servers, agents, hooks and
	 * memory. On a wide screen a side nav picks one; on a phone a list of the
	 * four drills into one, with a back button to the list (no tabs hide off
	 * the side). A project not on this worker has none to edit here, so it
	 * offers the setup instead.
	 */
	import { tick } from 'svelte';
	import { Bot, Brain, ChevronLeft, ChevronRight, Plus, Server, Zap } from 'lucide-svelte';
	import { EmptyDetail } from '$lib/components/layout/view';
	import type ProjectMcpTabType from './ProjectMcpTab.svelte';
	import ProjectMcpTab from './ProjectMcpTab.svelte';
	import ProjectAgentsTab from './ProjectAgentsTab.svelte';
	import ProjectHooksTab from './ProjectHooksTab.svelte';
	import ProjectMemoryTab from './ProjectMemoryTab.svelte';
	import './project-tabs.css';

	interface Props {
		/** This worker's id for the project; null when it isn't set up here. */
		projectId: string | null;
		projectName: string;
		selfName: string;
		stacked?: boolean;
		/** The resource open now (null on a phone: the list). */
		section?: ResourceId | null;
		/** Set the project up on this worker (remote-only projects). */
		onSetUpHere?: () => void;
	}

	let { projectId, projectName, selfName, stacked = false, section = $bindable(null), onSetUpHere }: Props = $props();

	const RESOURCES: { id: ResourceId; label: string; hint: string; icon: typeof Server }[] = [
		{ id: 'mcp', label: 'MCP servers', hint: 'Tools this project can use', icon: Server },
		{ id: 'agents', label: 'Agents', hint: 'Specialists for one job', icon: Bot },
		{ id: 'hooks', label: 'Hooks', hint: 'Commands run around tool use', icon: Zap },
		{ id: 'memory', label: 'Memory', hint: 'Instruction files for its chats', icon: Brain }
	];

	let mcpTab = $state<ProjectMcpTabType>();
	/** On a wide screen one resource is always open. */
	const shown = $derived<ResourceId | null>(section ?? (stacked ? null : 'mcp'));
	const current = $derived(RESOURCES.find((r) => r.id === shown) ?? null);

	async function open(id: ResourceId) {
		section = id;
		if (!stacked) return;
		await tick();
		document.querySelector<HTMLElement>('[data-ui-project-resources] .pr-back')?.focus();
	}

	async function back() {
		const from = section;
		section = null;
		await tick();
		if (from) document.getElementById(`pr-row-${from}`)?.focus();
	}
</script>

<div class="pr" class:with-nav={!stacked && !!projectId} data-ui-project-resources={shown ?? 'list'}>
	{#if !projectId}
		<EmptyDetail title={`${projectName} isn't on ${selfName} yet`} message="Set it up here to edit its MCP servers, agents, hooks and memory.">
			{#if onSetUpHere}<button class="st-btn primary" onclick={onSetUpHere}>Set up here</button>{/if}
		</EmptyDetail>
	{:else if stacked && !current}
		<ul class="pw-list" aria-label="Project resources">
			{#each RESOURCES as r (r.id)}
				<li>
					<button id="pr-row-{r.id}" type="button" class="pw-list-row" onclick={() => open(r.id)}>
						<r.icon class="pw-list-icon" aria-hidden="true" />
						<span class="pw-list-main"><span class="pw-list-name">{r.label}</span><span class="pw-list-sub">{r.hint}</span></span>
						<ChevronRight class="pw-list-chev" aria-hidden="true" />
					</button>
				</li>
			{/each}
		</ul>
	{:else if current}
		{#if stacked}
			<button type="button" class="st-btn pr-back" onclick={back}><ChevronLeft aria-hidden="true" />Resources</button>
		{:else}
			<nav class="pr-nav" aria-label="Project resources">
				{#each RESOURCES as r (r.id)}
					<button type="button" aria-current={r.id === current.id ? 'page' : undefined} onclick={() => open(r.id)}><r.icon aria-hidden="true" />{r.label}</button>
				{/each}
			</nav>
		{/if}
		<section class="pr-main" aria-labelledby="pr-title">
			<div class="pr-top">
				<h3 id="pr-title">{current.label}</h3>
				{#if current.id === 'mcp'}<button class="st-btn" onclick={() => mcpTab?.openAddForm()}><Plus />Add server</button>{/if}
			</div>
			{#key projectId}
				{#if current.id === 'mcp'}<ProjectMcpTab bind:this={mcpTab} {projectId} />
				{:else if current.id === 'agents'}<ProjectAgentsTab {projectId} />
				{:else if current.id === 'hooks'}<ProjectHooksTab {projectId} />
				{:else}<ProjectMemoryTab {projectId} />{/if}
			{/key}
		</section>
	{/if}
</div>
