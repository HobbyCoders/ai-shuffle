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
	import { workerChip } from './project-workers-view';
	import { workerDisplayName } from '$lib/components/ui/worker-name';

	interface Props {
		workers: ProjectWorker[];
		kind: ProjectKind | null;
		projectName: string;
		onSetup?: (worker: ProjectWorker) => void;
		onOpen?: (worker: ProjectWorker) => void;
	}

	let { workers, kind, projectName, onSetup, onOpen }: Props = $props();

	const chips = $derived(workers.map((w) => ({ worker: w, chip: workerChip({ ...w, device_name: workerDisplayName(w.device_name, w.is_self ? 'Local' : 'Worker') }, kind) })));

	function activate(e: MouseEvent, worker: ProjectWorker, actionable: boolean) {
		e.stopPropagation();
		if (actionable && onSetup) onSetup(worker);
		else onOpen?.(worker);
	}
</script>

	<div class="wchips" role="group" aria-label="Workers with {projectName}">
		{#each chips as { worker, chip } (worker.install_id)}
			<button
				type="button"
				class="wchip wchip--{chip.tone}"
				title={chip.title}
				aria-label="{chip.text}: {chip.actionable ? `not set up — set ${projectName} up on ${workerDisplayName(worker.device_name, worker.is_self ? 'Local' : 'Worker')}` : chip.title}"
				data-worker-state={worker.state}
				onclick={(e) => activate(e, worker, chip.actionable)}
				onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); }}
			>
				<i class="d" aria-hidden="true"></i><span class="t">{chip.text}</span>
			</button>
		{/each}
	</div>

<style>
	.wchips { display: flex; gap: 6px; flex-wrap: wrap; min-width: 0; }
	.wchip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; min-height: 16px; padding: 0; border: 0; border-radius: var(--st-radius-sm); background: transparent; color: var(--st-mfg); font: inherit; cursor: pointer; }
	.wchip:hover, .wchip:active { color: var(--st-fg); }
	.wchip:focus-visible { outline: 2px solid var(--st-primary); outline-offset: 4px; }
	@media (pointer: coarse), (max-width: 600px) { .wchip { position: relative; } .wchip::before { content: ''; position: absolute; inset: -12px 0; } }
	.wchip .t { overflow: hidden; text-overflow: ellipsis; }
	.d { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--st-success); }
	.wchip--missing .d { background: transparent; border: 1px solid var(--st-mfg); }
	.wchip--offline .d, .wchip--unknown .d { background: var(--st-mfg); }
	.wchip--failed .d { background: var(--st-destructive); }
	.wchip--busy .d { background: transparent; width: 9px; height: 9px; border: 2px solid var(--st-mfg); border-right-color: transparent; animation: worker-spin 0.8s linear infinite; }
	@keyframes worker-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .wchip--busy .d { animation: none; } }
</style>
