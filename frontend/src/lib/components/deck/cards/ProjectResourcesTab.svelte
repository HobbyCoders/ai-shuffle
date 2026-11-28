<script lang="ts" module>
	export type ResourceId = 'mcp' | 'agents' | 'hooks' | 'memory';
</script>

<script lang="ts">
	/**
	 * The project's resources on this worker: MCP servers, agents, hooks and
	 * memory. One switch picks which of the four shows, at every width. A
	 * project not on this worker has none to edit here, so it offers the setup
	 * instead.
	 */
	import { EmptyDetail, Segmented } from '$lib/components/layout/view';
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
		/** The resource open now. */
		section?: ResourceId;
		/** Set the project up on this worker (remote-only projects). */
		onSetUpHere?: () => void;
	}

	let { projectId, projectName, selfName, section = $bindable('mcp'), onSetUpHere }: Props = $props();

	const RESOURCES: { value: ResourceId; label: string; title: string }[] = [
		{ value: 'mcp', label: 'MCP', title: 'MCP servers: tools this project can use' },
		{ value: 'agents', label: 'Agents', title: 'Agents: specialists for one job' },
		{ value: 'hooks', label: 'Hooks', title: 'Hooks: commands run around tool use' },
		{ value: 'memory', label: 'Memory', title: 'Memory: instruction files for its chats' }
	];
</script>

<div class="pr" data-ui-project-resources={projectId ? section : 'none'}>
	{#if !projectId}
		<EmptyDetail title={`${projectName} isn't on ${selfName} yet`} message="Set it up here to edit its MCP servers, agents, hooks and memory.">
			{#if onSetUpHere}<button class="st-btn primary" onclick={onSetUpHere}>Set up here</button>{/if}
		</EmptyDetail>
	{:else}
		<div class="pr-switch"><Segmented label="Project resources" options={RESOURCES} bind:value={section} /></div>
		{#key projectId}
			{#if section === 'mcp'}<ProjectMcpTab {projectId} />
			{:else if section === 'agents'}<ProjectAgentsTab {projectId} />
			{:else if section === 'hooks'}<ProjectHooksTab {projectId} />
			{:else}<ProjectMemoryTab {projectId} />{/if}
		{/key}
	{/if}
</div>
