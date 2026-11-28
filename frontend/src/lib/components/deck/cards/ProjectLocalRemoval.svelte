<script lang="ts">
	/**
	 * "Remove project and chats" for this worker's copy. It asks this worker
	 * what the removal deletes when the confirmation opens (the project's
	 * session count spans every worker; the removal does not), keeps Remove
	 * disabled until that count arrives, and lists the worktree folders that
	 * stay on disk. It lives in this worker's details in the Workers tab, or
	 * in the Settings tab when the page has no Workers tab.
	 */
	import { tick, untrack } from 'svelte';
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
		/**
		 * The Settings tab sent the owner here: each new non-zero request id opens
		 * the confirmation with a fresh count (a second request after Cancel asks again).
		 */
		ask?: number;
		/** The request was taken: clear it so a remount does not ask again. */
		onAsked?: () => void;
		/**
		 * Whether the confirmation is open. Bind it from a parent that remounts
		 * this row (a worker's details closing and reopening) so an open
		 * confirmation survives; Cancel clears it, so a cancelled one never
		 * reopens.
		 */
		confirming?: boolean;
		/**
		 * The removal request is running, and why the last one failed. Bind them
		 * with `confirming` so a remount mid-request keeps Remove disabled (no
		 * second request) and shows that request's failure in the new row.
		 */
		removing?: boolean;
		error?: string | null;
		onRemoved?: () => void;
	}

	let {
		projectId, projectName, where, ask = 0, onAsked,
		confirming = $bindable(false), removing = $bindable(false), error = $bindable(null),
		onRemoved
	}: Props = $props();

	let preview = $state<ProjectRemovalPreview | null>(null);
	let pending = $state(false);
	let askSeq = 0;
	let trigger = $state<HTMLButtonElement | null>(null);

	async function askRemove(fresh = true) {
		const mine = ++askSeq;
		confirming = true;
		preview = null;
		pending = true;
		if (fresh) error = null;
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
		if (removing) return;
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

	// Mounted with the confirmation already open (the parent kept it across a
	// remount of this row): count again, behind a disabled Remove, rather than
	// reuse a number from before the remount. A request still running keeps
	// Remove disabled through `removing`; a failure already shown stays shown.
	if (untrack(() => confirming)) void askRemove(false);

	let asked = 0;
	$effect(() => {
		if (ask && ask !== asked) { asked = ask; void askRemove(); untrack(() => onAsked?.()); }
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
		<button bind:this={trigger} class="st-btn danger" disabled={removing} title={`Remove ${projectName} and its chats from ${where ?? 'this worker'}`} onclick={() => askRemove()}>Remove project and chats</button>
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
