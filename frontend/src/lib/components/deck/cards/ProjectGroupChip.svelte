<script lang="ts">
	/**
	 * The project's group, as a chip beside its name. Its popover picks a
	 * group ("No group" takes just this project out of its group), creates
	 * one, and, set apart at the end, deletes the current group. Deleting a
	 * group ungroups every project in it, so it asks first and names how many.
	 */
	import { tick } from 'svelte';
	import { Check, ChevronDown, Folders, Plus, Trash2 } from 'lucide-svelte';
	import { groups as groupsStore } from '$lib/stores/groups.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { floatingMenu } from '$lib/components/ui/floating-menu';
	import { groupDeleteMessage, groupMemberCount } from './project-groups';
	import '$lib/components/ui/surfaces.css';
	import './project-tabs.css';

	interface Props {
		projectId: string;
		projectName: string;
	}

	let { projectId, projectName }: Props = $props();

	const groups = $derived($groupsStore);
	const assignments = $derived(groups?.projects?.assignments ?? {});
	const current = $derived(assignments[projectId] ?? '');
	const names = $derived((groups?.projects?.groups ?? []).map((g) => g.name));

	let open = $state(false);
	let trigger = $state<HTMLButtonElement | null>(null);
	let menu = $state<HTMLDivElement | null>(null);
	let creating = $state(false);
	let newName = $state('');
	let pendingDelete = $state<{ name: string; members: number } | null>(null);

	async function show() {
		open = true;
		creating = false;
		await tick();
		menu?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus();
	}

	function hide(focusTrigger = true) {
		open = false;
		creating = false;
		newName = '';
		if (focusTrigger) void tick().then(() => trigger?.focus());
	}

	function pick(name: string) {
		if (name) groupsStore.assignToGroup('projects', projectId, name);
		else groupsStore.removeFromGroup('projects', projectId);
		hide();
	}

	async function startCreate() {
		creating = true;
		newName = '';
		await tick();
		menu?.querySelector<HTMLInputElement>('input[aria-label="Group name"]')?.focus();
	}

	function confirmCreate() {
		const name = newName.trim();
		if (!name) return;
		groupsStore.createGroup('projects', name);
		groupsStore.assignToGroup('projects', projectId, name);
		hide();
	}

	function askDelete() {
		if (!current) return;
		pendingDelete = { name: current, members: groupMemberCount(assignments, current) };
		hide(false);
	}

	function confirmDelete() {
		const pending = pendingDelete;
		pendingDelete = null;
		if (pending) groupsStore.deleteGroup('projects', pending.name);
		void tick().then(() => trigger?.focus());
	}

	function onWindowPointer(e: PointerEvent) {
		if (!open) return;
		const target = e.target as Node | null;
		if (target && (trigger?.contains(target) || menu?.contains(target))) return;
		hide(false);
	}

	function onMenuKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			if (creating) { creating = false; return; }
			hide();
			return;
		}
		if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
		if ((e.target as HTMLElement | null)?.tagName === 'INPUT') return;
		const items = [...(menu?.querySelectorAll<HTMLElement>('[role^="menuitem"]') ?? [])];
		const at = items.indexOf(document.activeElement as HTMLElement);
		const next = items[(at + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length];
		e.preventDefault();
		next?.focus();
	}
</script>

<svelte:window onpointerdown={onWindowPointer} />
<button bind:this={trigger} type="button" class="project-chip" aria-haspopup="menu" aria-expanded={open}
	aria-label={`Group: ${current || 'No group'}. Change the group for ${projectName}`}
	onclick={() => (open ? hide() : void show())}>
	<Folders aria-hidden="true" /><span class="project-chip-label">Group</span><span class="project-chip-value">{current || 'No group'}</span><ChevronDown aria-hidden="true" />
</button>
{#if open}
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div bind:this={menu} class="settings-shell project-pop" role="menu" tabindex="-1" aria-label={`Group for ${projectName}`}
		use:floatingMenu={{ anchor: trigger, align: 'start', gap: 6 }} onkeydown={onMenuKeydown}>
		<div class="project-pop-title">Group for {projectName}</div>
		{#each ['', ...names] as name (name)}
			<button type="button" class="project-pop-item" role="menuitemradio" aria-checked={current === name} onclick={() => pick(name)}>
				<span class="project-pop-check">{#if current === name}<Check aria-hidden="true" />{/if}</span>
				<span class="project-pop-text">{name || 'No group'}</span>
				{#if name}<span class="project-pop-sub">{groupMemberCount(assignments, name)}</span>{/if}
			</button>
		{/each}
		<hr />
		{#if creating}
			<form class="project-pop-form" onsubmit={(e) => { e.preventDefault(); confirmCreate(); }}>
				<input class="ui-input" aria-label="Group name" placeholder="Group name" bind:value={newName} />
				<button class="st-btn primary" disabled={!newName.trim()}>Create</button>
			</form>
		{:else}
			<button type="button" class="project-pop-item" role="menuitem" onclick={startCreate}><span class="project-pop-check"><Plus aria-hidden="true" /></span><span class="project-pop-text">New group</span></button>
		{/if}
		{#if current}
			<hr />
			<button type="button" class="project-pop-item danger" role="menuitem" onclick={askDelete}><span class="project-pop-check"><Trash2 aria-hidden="true" /></span><span class="project-pop-text">Delete group "{current}"…</span></button>
		{/if}
	</div>
{/if}
<ConfirmDialog open={!!pendingDelete} title="Delete group" message={pendingDelete ? `${groupDeleteMessage(pendingDelete.name, pendingDelete.members)}\nTo take only ${projectName} out, choose No group instead.` : ''}
	confirmLabel="Delete group" danger onConfirm={confirmDelete} onCancel={() => { pendingDelete = null; void tick().then(() => trigger?.focus()); }} />
