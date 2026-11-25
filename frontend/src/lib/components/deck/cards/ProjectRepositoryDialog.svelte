<script lang="ts">
	/**
	 * "Change repository on all workers": points the project at another
	 * repository (a new one or a fork) on every worker at once
	 * (PUT /projects/catalog/repository). It stays one project. After the
	 * switch the dialog says what happened on each worker.
	 */
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import Switch from '$lib/components/settings/rows/Switch.svelte';
	import { changeProjectRepository, type ProjectRef } from '$lib/api/project-workers';
	import { appData } from '$lib/stores/app-data.svelte';
	import { repositoryChangeSummary } from './project-workers-view';
	import ProjectSheet from './ProjectSheet.svelte';
	import './project-tabs.css';

	interface Props {
		projectRef: ProjectRef;
		/** The current repository's display form, if any. */
		remote: string | null;
		onClose: () => void;
		onChanged?: () => void;
	}

	let { projectRef, remote, onClose, onChanged }: Props = $props();

	let url = $state('');
	let keepUpstream = $state(false);
	let error = $state<string | null>(null);
	let busy = $state(false);
	let result = $state<{ ok: boolean; lines: string[] } | null>(null);
	let closed = false;

	async function save() {
		const value = url.trim();
		if (!value || busy) return;
		busy = true;
		error = null;
		try {
			const res = await changeProjectRepository(projectRef, value, keepUpstream);
			if (closed) return;
			result = repositoryChangeSummary(res);
			if (res.workers.some((w) => w.is_self)) void appData.loadProjectCatalog(performance.now());
			onChanged?.();
		} catch (e) {
			if (closed) return;
			const err = e as { detail?: unknown; message?: string };
			error = typeof err.detail === 'string' ? err.detail : err.message || "Couldn't change the repository.";
		} finally {
			busy = false;
		}
	}

	function close() {
		closed = true;
		onClose();
	}
</script>

<ProjectSheet title="Change repository on all workers" initial="[data-change-repo-input] input" onClose={close}>
	{#if result}
		<p class="project-repo-result" role="status" data-repo-result={result.ok ? 'ok' : 'partial'}>{result.lines.join(' ')}</p>
	{:else}
		<form id="project-repo-form" class="project-repo-form" onsubmit={(event) => { event.preventDefault(); void save(); }}>
			<p class="project-muted">A new repository or your fork. Every worker's copy switches to it and fetches it; branches, files and uncommitted work stay as they are. Workers that are offline switch when they come back. It stays one project.</p>
			<div class="project-repo-current"><span class="project-muted">Current</span><span class="st-mono">{remote || 'None'}</span></div>
			<div data-change-repo-input><TextField bind:value={url} width="full" mono label="New repository URL" placeholder="https://github.com/example/project.git" disabled={busy} /></div>
			{#if remote}
				<div class="project-repo-upstream">
					<div><div>Keep the current one as upstream</div><div class="project-muted">For a fork: {remote} stays reachable as the upstream remote, so you can still pull from it.</div></div>
					<Switch bind:checked={keepUpstream} disabled={busy} label="Keep the current one as upstream" />
				</div>
			{/if}
			{#if error}<p class="project-error" role="alert">{error}</p>{/if}
		</form>
	{/if}
	{#snippet footer()}
		{#if result}<button type="button" class="ui-button primary" onclick={close}>Done</button>
		{:else}
			<button type="button" class="ui-button" disabled={busy} onclick={close}>Cancel</button>
			<button type="submit" form="project-repo-form" class="ui-button primary" disabled={busy || !url.trim()}>{busy ? 'Switching…' : 'Switch repository'}</button>
		{/if}
	{/snippet}
</ProjectSheet>
