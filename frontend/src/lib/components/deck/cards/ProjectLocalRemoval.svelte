<script lang="ts">
	/**
	 * "Remove project and chats" for this worker's copy. It asks this worker
	 * what the removal deletes when the confirmation opens (the project's
	 * session count spans every worker; the removal does not), keeps Remove
	 * disabled until that count arrives, and lists the worktree folders that
	 * stay on disk. It lives in this worker's row of the worker table, or in
	 * Project settings when the page has no worker table.
	 */
	import { tick } from 'svelte';
	import { SettingRow } from '$lib/components/layout/view';
	import { appData } from '$lib/stores/app-data.svelte';
	import { getProjectRemovalPreview, type ProjectRemovalPreview } from '$lib/api/project-workers';
	import { removalConfirmText } from './project-removal';
	import './project-tabs.css';

	interface Props {
		projectId: string;
		projectName: string;
		/** The worker's name, for the question; null names no worker. */
		where: string | null;
		/** Open the confirmation as soon as this mounts (Project settings sent the owner here). */
		ask?: boolean;
		onRemoved?: () => void;
	}

	let { projectId, projectName, where, ask = false, onRemoved }: Props = $props();

	let confirming = $state(false);
	let preview = $state<ProjectRemovalPreview | null>(null);
	let pending = $state(false);
	let removing = $state(false);
	let error = $state<string | null>(null);
	let askSeq = 0;
	let trigger = $state<HTMLButtonElement | null>(null);

	async function askRemove() {
		const mine = ++askSeq;
		confirming = true;
		preview = null;
		pending = true;
		error = null;
		try {
			const res = await getProjectRemovalPreview(projectId);
			if (mine === askSeq) preview = res;
		} catch {
			// The confirmation still opens; it names no number.
		} finally {
			if (mine === askSeq) pending = false;
		}
	}

	function cancel() {
		askSeq++;
		confirming = false;
		preview = null;
		pending = false;
		void tick().then(() => trigger?.focus());
	}

	async function remove() {
		removing = true;
		error = null;
		try {
			await appData.deleteProject(projectId);
			askSeq++;
			confirming = false;
			onRemoved?.();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete project';
		} finally {
			removing = false;
		}
	}

	let asked = false;
	$effect(() => {
		if (ask && !asked) { asked = true; void askRemove(); }
	});

	const hint = $derived(!confirming
		? `Removes the project and its chats from ${where ?? 'this worker'}. Files stay on disk.`
		: pending ? 'Counting the chats this removes…'
		: removalConfirmText(projectName, where, { chats: preview?.chats ?? null, worktrees: preview?.worktrees.length ?? null }));
</script>

<SettingRow wrapHint label="Remove project" {hint} expanded={(confirming && preview?.worktrees.length) || error ? details : undefined}>
	{#if confirming}
		<button class="st-btn" disabled={removing} onclick={cancel}>Cancel</button>
		<button class="st-btn danger" disabled={removing || pending} onclick={remove}>{removing ? 'Removing…' : 'Remove'}</button>
	{:else}
		<button bind:this={trigger} class="st-btn danger" title={`Remove ${projectName} and its chats from ${where ?? 'this worker'}`} onclick={askRemove}>Remove project and chats</button>
	{/if}
</SettingRow>
{#snippet details()}
	{#if error}<p class="project-error" role="alert">{error}</p>{/if}
	{#if confirming && preview?.worktrees.length}
		<details class="kept-worktrees" data-kept-worktrees>
			<summary>Worktree folders that stay on disk</summary>
			<ul>{#each preview.worktrees as path (path)}<li class="st-mono">{path}</li>{/each}</ul>
		</details>
	{/if}
{/snippet}
