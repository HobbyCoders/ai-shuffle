<script lang="ts">
	/**
	 * Separate private backups from reviewable sharing exports; never fall back to raw.
	 *
	 * `menu`: labelled rows for the desktop chat actions dropdown.
	 * `sheet`: the same rows sized for the phone's chat actions sheet; they sit
	 * between that sheet's other rows, so each draws its own top divider.
	 */
	import { Download, FileDown, LoaderCircle } from 'lucide-svelte';
	import { exportSession } from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';

	let { sessionId, menu = false, sheet = false, disabled = false, onStart }: {
		sessionId: string;
		menu?: boolean;
		sheet?: boolean;
		disabled?: boolean;
		onStart?: () => void;
	} = $props();
	let pending = $state<'backup' | 'sharing' | null>(null);
	const labelled = $derived(menu || sheet);
	const iconSize = $derived(sheet ? 17 : 16);
	const privateNote = 'Raw JSONL may contain secrets. Keep this backup private; do not share.';
	const sharingNote = 'Recognized secrets removed; review content before sharing. Not a raw backup.';

	async function download(kind: 'backup' | 'sharing') {
		if (!sessionId || pending) return;
		pending = kind;
		onStart?.();
		try {
			if (kind === 'sharing') {
				await exportSession(sessionId, 'markdown', { shareSafe: true });
				notify(sharingNote, { severity: 'success' });
			} else {
				await exportSession(sessionId, 'jsonl');
			}
		} catch (err) {
			const detail = (err as { detail?: string })?.detail;
			notify(detail || (kind === 'sharing' ? 'Failed to export for sharing. Private backup was not downloaded.' : 'Failed to download private backup'), { severity: 'error' });
		} finally {
			pending = null;
		}
	}
</script>

<div class="exports" class:menu class:sheet aria-label="Chat exports">
	<button type="button" class="export-btn"disabled={disabled || !!pending || !sessionId} aria-busy={pending === 'backup'}
		aria-label="Download private backup (.jsonl)" title={privateNote} onclick={() => download('backup')}>
		{#if pending === 'backup'}<LoaderCircle class="export-spinner" size={iconSize} strokeWidth={1.5} aria-hidden="true" />{:else}<Download size={iconSize} strokeWidth={1.5} aria-hidden="true" />{/if}
		{#if labelled}<span class="copy"><span class="label">{pending === 'backup' ? 'Downloading private backup…' : 'Download private backup'}</span><span class="note">.jsonl · Raw history; may contain secrets. Do not share.</span></span>{/if}
	</button>
	<button type="button" class="export-btn"disabled={disabled || !!pending || !sessionId} aria-busy={pending === 'sharing'}
		aria-label="Export for sharing (.md)" title={sharingNote} onclick={() => download('sharing')}>
		{#if pending === 'sharing'}<LoaderCircle class="export-spinner" size={iconSize} strokeWidth={1.5} aria-hidden="true" />{:else}<FileDown size={iconSize} strokeWidth={1.5} aria-hidden="true" />{/if}
		{#if labelled}<span class="copy"><span class="label">{pending === 'sharing' ? 'Exporting for sharing…' : 'Export for sharing'}</span><span class="note">.md · Recognized secrets removed; review before sharing.</span></span>{/if}
	</button>
</div>

<style>
	.exports { display: flex; align-items: center; gap: 2px; }
	.export-btn { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; flex-shrink: 0; padding: 0; background: transparent; border: none; border-radius: calc(var(--radius, 0.75rem) * 0.5); color: var(--muted-foreground); font: inherit; cursor: pointer; transition: background-color 150ms ease, color 150ms ease; -webkit-tap-highlight-color: transparent; }
	.export-btn:hover:not(:disabled) { background: var(--hover-overlay); color: var(--foreground); }
	.export-btn:active:not(:disabled) { background: var(--hover-overlay); transform: translateY(1px); }
	.export-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.export-btn:disabled { opacity: 0.5; cursor: default; }
	.export-btn :global(svg) { flex: none; }
	.copy { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
	.label { color: var(--foreground); font-size: 13px; font-weight: 400; line-height: 1.35; }
	.note { font-size: 11px; line-height: 1.5; }
	/* Desktop dropdown: lines up with the menu's own text rows (Rename, Fork). */
	.menu, .sheet { flex-direction: column; align-items: stretch; gap: 0; }
	.menu .export-btn { width: 100%; height: auto; min-height: 44px; justify-content: flex-start; gap: 9px; padding: 7px 10px; border-radius: var(--menu-row-radius, 6px); text-align: left; }
	/* Phone sheet: matches SessionTitleMenu's .sheet-row. */
	.sheet .export-btn { width: 100%; height: auto; min-height: 52px; justify-content: flex-start; gap: 12px; padding: 8px 14px; border-top: 1px solid var(--border-subtle, var(--border)); border-radius: 0; text-align: left; }
	.sheet .export-btn:disabled { opacity: 0.45; cursor: not-allowed; }
	.sheet .label { font-size: 14px; }
	.sheet .note { font-size: 12px; line-height: 1.35; }
	.exports :global(.export-spinner) { animation: spin 1s linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (max-width: 639px) { .export-btn { width: 44px; height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .export-btn { transition: none; } .exports :global(.export-spinner) { animation: none; } }
</style>
