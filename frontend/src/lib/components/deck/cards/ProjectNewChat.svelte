<script lang="ts" module>
	import type { ProjectWorker } from '$lib/api/project-workers';

	/** Where "New chat" starts by default: this worker when it has the project, else the first online worker that does. */
	export function defaultChatWorker(workers: ProjectWorker[]): ProjectWorker | null {
		const usable = workers.filter((w) => w.online && w.state === 'ready');
		return usable.find((w) => w.is_self) ?? usable[0] ?? null;
	}
</script>

<script lang="ts">
	/**
	 * The project's "New chat" action: a split button that starts a chat on
	 * the default worker, and a menu of every worker (only the online ones
	 * holding the project can be picked; the others say why).
	 * Without the worker overview the project is only known here, so it
	 * starts the chat on this worker.
	 */
	import { tick } from 'svelte';
	import { ChevronDown, MessageSquarePlus } from 'lucide-svelte';
	import WorkerMenuList, { type WorkerMenuRow } from '$lib/components/ui/WorkerMenuList.svelte';
	import { floatingMenu } from '$lib/components/ui/floating-menu';
	import { workerDisplayName } from '$lib/components/ui/worker-name';
	import { activeSession } from '$lib/stores/active-session';
	import { layout } from '$lib/stores/layout.svelte';
	import { workerChip } from './project-workers-view';
	import type { ProjectKind } from '$lib/api/project-workers';
	import './project-tabs.css';

	interface Props {
		/** null: no worker overview (only this worker is known). */
		workers: ProjectWorker[] | null;
		kind: ProjectKind | null;
		localProjectId: string | null;
		projectV2Id: string | null;
	}

	let { workers, kind, localProjectId, projectV2Id }: Props = $props();

	const named = $derived((workers ?? []).map((w) => ({ ...w, device_name: workerDisplayName(w.device_name, w.is_self ? 'Local' : 'Worker') })));
	const target = $derived(workers ? defaultChatWorker(named) : null);
	const canStart = $derived(workers ? !!target : !!localProjectId);
	const label = $derived(workers && target && (!target.is_self || named.length > 1) ? `New chat on ${target.device_name}` : 'New chat');

	let open = $state(false);
	let wrap = $state<HTMLDivElement | null>(null);
	let caret = $state<HTMLButtonElement | null>(null);
	let menu = $state<HTMLDivElement | null>(null);

	const rows = $derived(named.map((w): WorkerMenuRow => {
		const ok = w.online && w.state === 'ready';
		const chip = workerChip(w, kind);
		return {
			id: w.install_id,
			label: w.device_name,
			online: w.online,
			disabled: !ok,
			title: ok ? `Start a chat on ${w.device_name}` : chip.title,
			note: ok ? (w.is_self ? 'This worker' : null) : !w.online ? 'offline' : w.state === 'failed' ? 'failed' : w.state === 'setting_up' ? 'setting up' : 'not set up'
		};
	}));

	function start(worker: ProjectWorker | null) {
		open = false;
		activeSession.newChat();
		activeSession.setProject(localProjectId ?? '', projectV2Id, { user: true });
		activeSession.setDeviceIntent(!worker || worker.is_self ? null : { id: worker.install_id, name: worker.device_name });
		layout.openChat(null);
	}

	async function toggle() {
		open = !open;
		if (!open) return;
		await tick();
		menu?.querySelector<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])')?.focus();
	}

	function onWindowPointer(e: PointerEvent) {
		if (!open) return;
		const node = e.target as Node | null;
		if (node && (wrap?.contains(node) || menu?.contains(node))) return;
		open = false;
	}

	function onMenuKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			open = false;
			caret?.focus();
			return;
		}
		if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
		const items = [...(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];
		const at = items.indexOf(document.activeElement as HTMLElement);
		e.preventDefault();
		items[(at + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
	}
</script>

<svelte:window onpointerdown={onWindowPointer} />
<div bind:this={wrap} class="project-split">
	<button type="button" class="st-btn primary" disabled={!canStart} title={canStart ? undefined : 'Set it up on a worker first'} onclick={() => start(target)}>
		<MessageSquarePlus aria-hidden="true" />{label}
	</button>
	{#if workers && named.length > 1}
		<button bind:this={caret} type="button" class="st-btn primary icon project-split-caret" aria-label="Choose a worker" title="Choose a worker" aria-haspopup="menu" aria-expanded={open} onclick={toggle}>
			<ChevronDown aria-hidden="true" />
		</button>
	{/if}
</div>
{#if open}
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div bind:this={menu} class="settings-shell project-pop" role="menu" tabindex="-1" aria-label="Start a chat on" use:floatingMenu={{ anchor: wrap, align: 'end', gap: 6 }} onkeydown={onMenuKeydown}>
		<div class="project-pop-title">Start a chat on</div>
		<WorkerMenuList items={rows} onSelect={(row) => start(named.find((w) => w.install_id === row.id) ?? null)} />
	</div>
{/if}
