<script lang="ts">
	/**
	 * ProjectWorkerChips — which workers hold a project, one chip per worker.
	 *
	 *   ready     solid, green dot
	 *   missing   dashed; a click sets the project up there (default folder)
	 *   busy      spinner ("Studio · cloning 40%")
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
				class="st-btn sm wchip wchip--{chip.tone}"
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
	.wchips { display: flex; gap: 6px; flex-wrap: wrap; min-width: 0; }
	.wchips--legend { gap: 8px 12px; color: var(--st-mfg); font-size: 12px; }
	.wchip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; }
	.wchip .t { overflow: hidden; text-overflow: ellipsis; }
	.d { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--st-success); }
	.wchip--missing .d { background: transparent; border: 1px solid var(--st-mfg); }
	.wchip--offline .d, .wchip--unknown .d { background: var(--st-mfg); }
	.wchip--failed .d { background: var(--st-destructive); }
	.wchip--busy .d { background: transparent; width: 9px; height: 9px; border: 2px solid var(--st-mfg); border-right-color: transparent; animation: worker-spin 0.8s linear infinite; }
	@keyframes worker-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .wchip--busy .d { animation: none; } }
</style>
