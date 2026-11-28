<script lang="ts">
	/**
	 * The project's group, as a row of its Identity settings (the same row an
	 * agent's folder uses). The select picks a group ("No group" takes just
	 * this project out of its group); the buttons beside it create one and
	 * delete the current one. Deleting a group ungroups every project in it,
	 * so it asks first and names how many.
	 */
	import { tick } from 'svelte';
	import { Check, FolderPlus, Trash2, X } from 'lucide-svelte';
	import { SettingRow, SelectField } from '$lib/components/layout/view';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { groups as groupsStore } from '$lib/stores/groups.svelte';
	import { groupDeleteMessage, groupMemberCount } from './project-groups';

	interface Props {
		projectId: string;
		projectName: string;
	}

	let { projectId, projectName }: Props = $props();

	const assignments = $derived($groupsStore?.projects?.assignments ?? {});
	const names = $derived(($groupsStore?.projects?.groups ?? []).map((g) => g.name));
	const current = $derived.by(() => {
		const name = assignments[projectId] ?? '';
		return name && names.includes(name) ? name : '';
	});

	let controls = $state<HTMLSpanElement | null>(null);
	let creating = $state(false);
	let newName = $state('');
	let pendingDelete = $state<{ name: string; members: number } | null>(null);

	/** Put the keyboard back on the row after one of its controls went away. */
	function refocus(selector: string) {
		void tick().then(() => controls?.querySelector<HTMLElement>(selector)?.focus());
	}

	function startCreate() {
		creating = true;
		newName = '';
		refocus('input');
	}

	function cancelCreate() {
		creating = false;
		newName = '';
		refocus('[data-group-new]');
	}

	function confirmCreate() {
		const name = newName.trim();
		if (!name) return;
		if (!names.includes(name)) groupsStore.createGroup('projects', name);
		groupsStore.assignToGroup('projects', projectId, name);
		creating = false;
		newName = '';
		refocus('select');
	}

	function pick(name: string) {
		if (name) groupsStore.assignToGroup('projects', projectId, name);
		else groupsStore.removeFromGroup('projects', projectId);
	}

	function confirmDelete() {
		const pending = pendingDelete;
		pendingDelete = null;
		if (pending) groupsStore.deleteGroup('projects', pending.name);
		refocus('select');
	}
</script>

<SettingRow label="Group" hint="Groups projects in the list.">
	<span class="project-controls" data-ui-project-group bind:this={controls}>
		{#if creating}
			<TextField bind:value={newName} oninput={(value) => (newName = value)} width={200} placeholder="Group name" label="Group name"
				onkeydown={(e) => {
					if (e.key === 'Enter') { e.preventDefault(); confirmCreate(); }
					else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelCreate(); }
				}} />
			<button type="button" class="st-btn icon sm" aria-label="Create group" title="Create group" disabled={!newName.trim()} onclick={confirmCreate}><Check /></button>
			<button type="button" class="st-btn ghost icon sm" aria-label="Cancel new group" title="Cancel" onclick={cancelCreate}><X /></button>
		{:else}
			<SelectField value={current} width={200} label={`Group for ${projectName}`}
				options={[{ value: '', label: 'No group' }, ...names.map((name) => ({ value: name, label: name }))]} onchange={pick} />
			<button type="button" class="st-btn icon sm" data-group-new aria-label="New group" title="New group" onclick={startCreate}><FolderPlus /></button>
			{#if current}
				<button type="button" class="st-btn ghost icon sm" aria-label={`Delete group ${current}`} title="Delete group"
					onclick={() => (pendingDelete = { name: current, members: groupMemberCount(assignments, current) })}><Trash2 /></button>
			{/if}
		{/if}
	</span>
</SettingRow>
<ConfirmDialog open={!!pendingDelete} title="Delete group" message={pendingDelete ? `${groupDeleteMessage(pendingDelete.name, pendingDelete.members)}\nTo take only ${projectName} out, choose No group instead.` : ''}
	confirmLabel="Delete group" danger onConfirm={confirmDelete} onCancel={() => { pendingDelete = null; refocus('select'); }} />
