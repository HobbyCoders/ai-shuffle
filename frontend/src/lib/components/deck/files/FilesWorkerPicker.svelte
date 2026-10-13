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
		class="fwp-chip"
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
	.fwp { position: relative; flex-shrink: 0; }

	/* The chip reads as the first segment of the path: machine / Workspace / … */
	.fwp-chip {
		display: inline-flex; align-items: center; gap: 7px;
		height: 30px; padding: 0 8px 0 7px; max-width: 260px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: 999px;
		font-size: 0.78rem; font-weight: 600; cursor: pointer;
		transition: border-color 140ms ease, background-color 140ms ease, box-shadow 140ms ease;
	}
	.fwp-chip:hover { background: var(--hover-overlay); }
	.fwp-chip:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.fwp-chip[aria-expanded='true'] { border-color: var(--ring); }
	/* Another machine's files: a tinted rim so it is never mistaken for local. */
	.fwp-chip.remote {
		border-color: color-mix(in oklch, var(--primary) 55%, var(--border));
		background: color-mix(in oklch, var(--primary) 9%, var(--card));
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 10%, transparent);
	}
	.fwp-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
	.fwp-tag {
		font-family: var(--font-mono, monospace); font-size: 0.64rem; font-weight: 500;
		letter-spacing: 0.04em; color: var(--muted-foreground); white-space: nowrap;
	}
	.fwp-chip :global(.fwp-caret) { color: var(--muted-foreground); flex-shrink: 0; transition: transform 160ms ease; }
	.fwp-chip[aria-expanded='true'] :global(.fwp-caret) { transform: rotate(180deg); }

	.fwp-ic { position: relative; display: grid; place-items: center; flex-shrink: 0; color: var(--muted-foreground); }
	.fwp-dot {
		position: absolute; right: -3px; bottom: -2px; width: 7px; height: 7px; border-radius: 50%;
		background: var(--success, #16a34a); box-shadow: 0 0 0 2px var(--card);
	}
	.fwp-dot.off { background: var(--muted-foreground); opacity: 0.8; }

	.fwp-menu {
		position: absolute; top: calc(100% + 6px); left: 0; z-index: 40;
		width: max-content; min-width: 240px; max-width: min(320px, calc(100vw - 32px));
		padding: 6px; border-radius: var(--radius, 0.75rem);
		background: var(--popover, var(--card)); color: var(--foreground);
		border: 1px solid var(--border);
		box-shadow: 0 12px 32px -12px rgb(0 0 0 / 0.35), 0 2px 6px rgb(0 0 0 / 0.08);
		animation: fwp-in 140ms cubic-bezier(0.2, 0.8, 0.2, 1);
		transform-origin: top left;
	}
	@keyframes fwp-in { from { opacity: 0; transform: translateY(-4px) scale(0.98); } }
	.fwp-head {
		padding: 6px 8px 6px; font-family: var(--font-mono, monospace); font-size: 0.64rem;
		text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted-foreground); font-weight: 600;
	}
	.fwp-item {
		display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px;
		padding: 6px 8px; border: none; border-radius: 8px; background: transparent;
		color: inherit; text-align: left; cursor: pointer; font-size: 0.8125rem;
		transition: background-color 110ms ease;
	}
	.fwp-item:hover:not([aria-disabled='true']), .fwp-item:focus-visible { background: var(--hover-overlay); outline: none; }
	.fwp-item:focus-visible { box-shadow: inset 0 0 0 2px var(--ring); }
	.fwp-item[aria-disabled='true'] { cursor: not-allowed; opacity: 0.55; }
	.fwp-item[aria-checked='true'] .fwp-item-name { font-weight: 650; }
	.fwp-text { display: flex; flex-direction: column; min-width: 0; flex: 1; }
	.fwp-item-name { font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.fwp-hint { font-family: var(--font-mono, monospace); font-size: 0.7rem; font-weight: 500; color: var(--muted-foreground); }
	.fwp-sub { font-size: 0.7rem; color: var(--muted-foreground); }
	.fwp-check { width: 16px; display: grid; place-items: center; color: var(--primary); flex-shrink: 0; }
	.fwp-sep { height: 1px; margin: 4px 6px; background: var(--border-subtle, var(--border)); }
	.fwp-note {
		display: flex; align-items: center; gap: 6px; padding: 10px 8px 8px;
		font-size: 0.75rem; color: var(--muted-foreground);
	}
	.fwp-retry {
		display: inline-flex; align-items: center; gap: 4px; margin-left: auto;
		background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.75rem;
	}
	.fwp-retry:hover { text-decoration: underline; }

	@media (max-width: 767px) {
		.fwp-chip { height: 34px; max-width: 200px; }
		.fwp-tag { display: none; }
	}
	@media (prefers-reduced-motion: reduce) {
		.fwp-menu { animation: none; }
		.fwp-chip :global(.fwp-caret) { transition: none; }
	}
</style>
