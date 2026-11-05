<script lang="ts">
	/**
	 * FilesWorkerPicker — the machine at the root of the Files breadcrumb.
	 *
	 * A chip that names the worker whose files are on screen and opens a short
	 * list of the owner's workers: this one ("This device") first, then paired
	 * peers with their online state. Offline peers stay listed but
	 * disabled, with the reason on the row. Keyed by device id, never by name.
	 */
	import { tick } from 'svelte';
	import { Server, Monitor, ChevronDown, Check, Loader2, RotateCw } from 'lucide-svelte';
	import { SELF_LABEL, type WorkerOption } from './files-workers';
	import { emptyRunnersCopy } from '$lib/components/layout/execution-mode-eligibility';

	interface Props {
		options: WorkerOption[];
		selected: WorkerOption;
		/** True while the worker list is being fetched. */
		loading?: boolean;
		/** The worker list could not be read. */
		failed?: boolean;
		onSelect: (key: string | null) => void;
		onRetry?: () => void;
	}

	let { options, selected, loading = false, failed = false, onSelect, onRetry }: Props = $props();

	let open = $state(false);
	let root = $state<HTMLDivElement | null>(null);
	let trigger = $state<HTMLButtonElement | null>(null);
	let menu = $state<HTMLDivElement | null>(null);
	const menuId = `fwp-menu-${Math.random().toString(36).slice(2, 8)}`;

	const peers = $derived(options.filter((o) => !o.isSelf));

	function items(): HTMLButtonElement[] {
		return Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]:not([aria-disabled="true"])') ?? []);
	}

	async function toggle() {
		open = !open;
		if (open) {
			await tick();
			const list = items();
			(list.find((el) => el.getAttribute('aria-checked') === 'true') ?? list[0])?.focus();
		}
	}

	function close(focusTrigger = true) {
		open = false;
		if (focusTrigger) trigger?.focus();
	}

	function pick(option: WorkerOption) {
		if (option.disabled) return;
		close();
		if (option.key !== selected.key) onSelect(option.key);
	}

	function onMenuKeydown(e: KeyboardEvent) {
		const list = items();
		const at = list.indexOf(document.activeElement as HTMLButtonElement);
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			list[(at + 1) % list.length]?.focus();
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			list[(at - 1 + list.length) % list.length]?.focus();
		} else if (e.key === 'Home') {
			e.preventDefault();
			list[0]?.focus();
		} else if (e.key === 'End') {
			e.preventDefault();
			list[list.length - 1]?.focus();
		} else if (e.key === 'Escape' || e.key === 'Tab') {
			if (e.key === 'Escape') e.preventDefault();
			close(e.key === 'Escape');
		}
	}

	function onWindowPointer(e: PointerEvent) {
		if (open && root && !root.contains(e.target as Node)) close(false);
	}

	function label(o: WorkerOption) {
		return o.hint ? `${o.name} (${o.hint})` : o.name;
	}
</script>

<svelte:window onpointerdown={onWindowPointer} />

<div class="fwp" bind:this={root}>
	<button
		bind:this={trigger}
		type="button"
		class="st-btn fwp-chip"
		class:remote={!selected.isSelf}
		aria-haspopup="menu"
		aria-expanded={open}
		aria-controls={menuId}
		aria-label="Worker: {label(selected)}{selected.isSelf ? ` (${SELF_LABEL.toLowerCase()})` : ''}. Change worker"
		title={selected.isSelf ? `Files on ${SELF_LABEL.toLowerCase()}` : `Files on ${label(selected)}`}
		onclick={toggle}
	>
		<span class="fwp-ic" aria-hidden="true">
			{#if selected.isSelf}<Monitor size={13} strokeWidth={1.8} />{:else}<Server size={13} strokeWidth={1.8} />{/if}
			<span class="fwp-dot" class:off={!selected.online}></span>
		</span>
		<span class="fwp-name">{selected.name}</span>
		{#if selected.isSelf}<span class="fwp-tag">{SELF_LABEL.toLowerCase()}</span>{:else if selected.hint}<span class="fwp-tag">{selected.hint}</span>{/if}
		<ChevronDown size={13} strokeWidth={2} class="fwp-caret" aria-hidden="true" />
	</button>

	{#if open}
		<!-- svelte-ignore a11y_interactive_supports_focus -->
		<div id={menuId} class="fwp-menu" role="menu" aria-label="Show files from" bind:this={menu} onkeydown={onMenuKeydown}>
			<div class="fwp-head">Show files from</div>
			{#each options as o (o.key ?? '__self__')}
				<button
					type="button"
					class="fwp-item"
					role="menuitemradio"
					aria-checked={o.key === selected.key}
					aria-disabled={o.disabled}
					tabindex="-1"
					title={o.disabled && o.reason ? `${o.name}: ${o.reason.toLowerCase()}` : undefined}
					onclick={() => pick(o)}
				>
					<span class="fwp-ic" aria-hidden="true">
						{#if o.isSelf}<Monitor size={14} strokeWidth={1.8} />{:else}<Server size={14} strokeWidth={1.8} />{/if}
						<span class="fwp-dot" class:off={!o.online}></span>
					</span>
					<span class="fwp-text">
						<span class="fwp-item-name">{o.name}{#if o.hint && !o.isSelf}<span class="fwp-hint"> · {o.hint}</span>{/if}</span>
						<span class="fwp-sub">{o.isSelf ? SELF_LABEL : o.reason ?? 'Online'}</span>
					</span>
					<span class="fwp-check" aria-hidden="true">{#if o.key === selected.key}<Check size={14} strokeWidth={2.2} />{/if}</span>
				</button>
				{#if o.isSelf}<div class="fwp-sep" role="separator"></div>{/if}
			{/each}
			{#if loading && peers.length === 0}
				<div class="fwp-note" role="status"><Loader2 size={13} class="fs-spin" /> Looking for workers…</div>
			{:else if failed && peers.length === 0}
				<div class="fwp-note">
					Couldn't list workers.
					{#if onRetry}<button type="button" class="fwp-retry" onclick={onRetry}><RotateCw size={12} /> Retry</button>{/if}
				</div>
			{:else if peers.length === 0}
				<div class="fwp-note">{emptyRunnersCopy}</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.fwp { position: relative; min-width: 0; flex: 1; }
	.fwp-chip { width: 100%; min-width: 0; justify-content: flex-start; }
	.fwp-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
	.fwp-tag { color: var(--st-mfg); font-size: 11px; white-space: nowrap; }
	.fwp-chip :global(.fwp-caret) { margin-left: auto; flex: none; color: var(--st-mfg); }
	.fwp-ic { position: relative; display: grid; place-items: center; flex: none; color: var(--st-mfg); }
	.fwp-dot { position: absolute; right: -3px; bottom: -2px; width: 6px; height: 6px; border-radius: 50%; background: var(--st-success); }
	.fwp-dot.off { background: var(--st-mfg); }
	.fwp-menu { position: absolute; top: calc(100% + 6px); left: 0; z-index: 40; width: 100%; min-width: 220px; max-width: calc(100vw - 32px); padding: 6px; border: 1px solid var(--menu-border); border-radius: var(--menu-radius); background: var(--st-popover); color: var(--st-fg); box-shadow: var(--menu-shadow); }
	.fwp-head { padding: 6px 8px; font-size: 12px; color: var(--st-mfg); }
	.fwp-item { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px; padding: 6px 8px; border: 0; border-radius: var(--menu-row-radius); background: transparent; color: inherit; text-align: left; cursor: pointer; font-size: 13px; }
	.fwp-item:hover:not([aria-disabled='true']), .fwp-item:focus-visible { background: var(--st-hover); }
	.fwp-item[aria-checked='true'] { background: var(--st-accent); }
	.fwp-item[aria-disabled='true'] { cursor: not-allowed; opacity: .55; }
	.fwp-text { display: flex; flex-direction: column; min-width: 0; flex: 1; }
	.fwp-item-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.fwp-hint, .fwp-sub { font-size: 12px; color: var(--st-mfg); }
	.fwp-check { width: 16px; display: grid; place-items: center; flex: none; color: var(--st-fg); }
	.fwp-sep { height: 1px; margin: 4px 6px; background: var(--st-border); }
	.fwp-note { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding: 8px; color: var(--st-mfg); font-size: 12px; }
	.fwp-retry { display: inline-flex; align-items: center; gap: 4px; background: transparent; color: var(--st-fg); border: 0; text-decoration: underline; }
	@media (max-width: 600px), (pointer: coarse) { .fwp-chip { min-height: 44px; } }
</style>
