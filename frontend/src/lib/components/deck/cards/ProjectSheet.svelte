<script lang="ts">
	/**
	 * ProjectSheet: the one modal surface the project page uses. A centred
	 * dialog by default; `side` makes it a full-height panel on the right
	 * (Project settings). Under 600px the shared `.ui-dialog` rules turn either
	 * into a bottom sheet, and `side` grows it to the full height.
	 *
	 * It is moved to <body> so a transformed ancestor cannot clip it, wraps
	 * its content in `.settings-shell` so the view kit's rows and buttons keep
	 * their tokens, traps focus, and closes on Escape or a scrim click.
	 */
	import type { Snippet } from 'svelte';
	import { X } from 'lucide-svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import '$lib/components/ui/surfaces.css';

	interface Props {
		title: string;
		subtitle?: string;
		side?: boolean;
		/** Live content (a worker's setup progress): on a phone the sheet keeps
		 *  one height, so a status change does not move its top edge. */
		steady?: boolean;
		/** Focused when it opens; defaults to the first control. */
		initial?: string;
		/** Asked as it closes: false when it closed to open something else, so
		 *  focus is not sent back to the control that opened it. */
		returnFocus?: () => boolean;
		onClose: () => void;
		children: Snippet;
		footer?: Snippet;
	}

	let { title, subtitle, side = false, steady = false, initial, returnFocus, onClose, children, footer }: Props = $props();
	const uid = $props.id();

	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return { destroy() { node.remove(); } };
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key !== 'Escape' || e.defaultPrevented) return;
		// The topmost dialog owns Escape: one opened inside this sheet (Set up Git)
		// closes itself and hands focus back, and this sheet stays open.
		const owner = e.target instanceof Element ? e.target.closest('[aria-modal="true"]') : null;
		if (owner && owner !== e.currentTarget) return;
		e.preventDefault();
		e.stopPropagation();
		onClose();
	}
</script>

<div class="settings-shell project-sheet-host" use:portal>
	<div class="ui-overlay ui-surface project-sheet" class:side class:steady role="dialog" aria-modal="true" aria-labelledby="{uid}-title" tabindex="-1"
		use:focusTrap={{ initial, returnFocus }} onkeydown={onKeydown}>
		<button class="ui-scrim" tabindex="-1" aria-label="Close" onclick={onClose}></button>
		<div class="ui-dialog project-sheet-panel">
			<header class="ui-dialog-header">
				<div class="project-sheet-heading">
					<h2 id="{uid}-title" class="ui-dialog-title">{title}</h2>
					{#if subtitle}<p class="ui-dialog-subtitle">{subtitle}</p>{/if}
				</div>
				<button type="button" class="ui-button icon ghost" aria-label="Close {title}" title="Close" onclick={onClose}><X size={16} /></button>
			</header>
			<div class="ui-dialog-body project-sheet-body">{@render children()}</div>
			{#if footer}<div class="ui-dialog-footer">{@render footer()}</div>{/if}
		</div>
	</div>
</div>

<style>
	.project-sheet-host { display: contents; }
	.project-sheet-heading { min-width: 0; }
	.project-sheet-body { padding-bottom: 16px; }
	/* The view kit's row groups carry their own top margin; the first one sits under the title. */
	.project-sheet-body > :global(:first-child) { margin-top: 0; }
	.project-sheet.side { place-items: stretch end; padding: 0; }
	.project-sheet.side .project-sheet-panel { --ui-dialog-width: 460px; height: 100%; max-height: 100%; border-radius: 0; }
	@media (max-width: 600px) {
		.project-sheet.side .project-sheet-panel { height: 100dvh; max-height: 100dvh; border-radius: 0; }
		.project-sheet.steady .project-sheet-panel { height: 92dvh; }
	}
</style>
