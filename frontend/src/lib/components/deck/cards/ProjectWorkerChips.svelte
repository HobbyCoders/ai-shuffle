<script lang="ts">
	/**
	 * ProjectWorkerChips — which workers hold a project, one chip per worker.
	 *
	 *   ready     solid, green dot
	 *   missing   dashed; a click sets the project up there (default folder)
	 *   busy      spinner ("GamingPC · cloning 40%")
	 *   offline   grey ("offline · has it", "offline", "queued")
	 *   failed    red; its title is the reason
	 *   unknown   quiet outline (the worker's catalog could not be read)
	 *
	 * Every chip is a real button with a full-sentence label: a missing chip
	 * starts a setup (`onSetup`), the others open the project's Workers tab
	 * (`onOpen`). Colours are theme tokens; the state never rests on colour
	 * alone (each has its own shape and words).
	 */
	import type { ProjectKind, ProjectWorker } from '$lib/api/project-workers';
	import { workerChip, type ChipTone } from './project-workers-view';

	interface Props {
		workers: ProjectWorker[];
		kind: ProjectKind | null;
		projectName: string;
		onSetup?: (worker: ProjectWorker) => void;
		onOpen?: (worker: ProjectWorker) => void;
		/** Legend mode: static examples, not buttons. */
		legend?: { tone: ChipTone; text: string }[];
	}

	let { workers, kind, projectName, onSetup, onOpen, legend }: Props = $props();

	const chips = $derived(workers.map((w) => ({ worker: w, chip: workerChip(w, kind) })));

	function activate(e: MouseEvent, worker: ProjectWorker, actionable: boolean) {
		e.stopPropagation();
		if (actionable && onSetup) onSetup(worker);
		else onOpen?.(worker);
	}
</script>

{#if legend}
	<div class="wchips wchips--legend" aria-hidden="true">
		{#each legend as item (item.tone)}
			<span class="wchip wchip--{item.tone}"><i class="d"></i>{item.text}</span>
		{/each}
	</div>
{:else}
	<div class="wchips" role="group" aria-label="Workers with {projectName}">
		{#each chips as { worker, chip } (worker.install_id)}
			<button
				type="button"
				class="wchip wchip--{chip.tone}"
				title={chip.title}
				aria-label="{chip.text}: {chip.actionable ? `not set up — set ${projectName} up on ${worker.device_name}` : chip.title}"
				data-worker-state={worker.state}
				onclick={(e) => activate(e, worker, chip.actionable)}
				onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); }}
			>
				<i class="d" aria-hidden="true"></i><span class="t">{chip.text}</span>
			</button>
		{/each}
	</div>
{/if}

<style>
	.wchips { display: flex; gap: 5px; flex-wrap: wrap; justify-content: flex-end; min-width: 0; }
	.wchips--legend { justify-content: flex-start; gap: 12px; }
	.wchip {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		min-height: 24px;
		max-width: 100%;
		padding: 2px 8px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 0.72rem;
		line-height: 1.2;
		white-space: nowrap;
		cursor: pointer;
		transition: color 120ms ease, border-color 120ms ease, background-color 120ms ease;
	}
	.wchips--legend .wchip { cursor: default; }
	.wchip .t { overflow: hidden; text-overflow: ellipsis; }
	button.wchip:hover { background: var(--hover-overlay); }
	button.wchip:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.d { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--success); }

	.wchip--missing { border-style: dashed; color: var(--muted-foreground); }
	.wchip--missing .d { width: 8px; height: 8px; background: transparent; border: 1.5px solid var(--muted-foreground); }
	button.wchip--missing:hover { color: var(--primary); border-color: var(--primary); background: color-mix(in oklch, var(--primary) 8%, transparent); }
	button.wchip--missing:hover .d { border-color: var(--primary); }

	.wchip--offline { color: var(--muted-foreground); }
	.wchip--offline .d { background: var(--muted-foreground); }

	.wchip--busy { color: var(--primary); border-color: color-mix(in oklch, var(--primary) 50%, transparent); }
	.wchip--busy .d {
		width: 9px;
		height: 9px;
		background: transparent;
		border: 2px solid var(--primary);
		border-right-color: transparent;
		animation: wchip-spin 0.8s linear infinite;
	}

	.wchip--failed { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 50%, transparent); }
	.wchip--failed .d { background: var(--destructive); }

	.wchip--unknown { color: var(--muted-foreground); border-style: dotted; }
	.wchip--unknown .d { background: transparent; border: 1.5px dotted var(--muted-foreground); }

	@keyframes wchip-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.wchip--busy .d { animation: none; border-right-color: var(--primary); opacity: 0.6; }
	}
</style>
