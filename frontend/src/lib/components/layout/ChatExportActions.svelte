<script lang="ts">
	/**
	 * Separate private backups from reviewable sharing exports; never fall back to raw.
	 *
	 * `page`: the two exports as a choice with one primary button under them,
	 * for a host page (the phone's Chat controls "Export chat" page). The
	 * button sits last so the host can pin it to the page's bottom edge.
	 */
	import { Check, Download, FileDown, LoaderCircle } from 'lucide-svelte';
	import { exportSession } from '$lib/api/sessions';
	import { notify } from '$lib/stores/notifications';

	let { sessionId, menu = false, page = false, onStart }: {
		sessionId: string;
		menu?: boolean;
		page?: boolean;
		onStart?: () => void;
	} = $props();
	/** The page layout's choice; sharing is the safe default. */
	let choice = $state<'backup' | 'sharing'>('sharing');
	let pending = $state<'backup' | 'sharing' | null>(null);
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

{#if page}
<div class="exports page">
	<div class="choices" role="radiogroup" aria-label="Export format">
		<button type="button" class="choice" role="radio" aria-checked={choice === 'sharing'} disabled={!!pending} onclick={() => (choice = 'sharing')}>
			<span class="choice-icon" aria-hidden="true"><FileDown size={17} strokeWidth={1.75} /></span>
			<span class="copy"><span class="label">Export for sharing</span><span class="note">.md · Recognized secrets removed; review before sharing.</span></span>
			<Check size={18} strokeWidth={2.4} class="choice-check" aria-hidden="true" />
		</button>
		<button type="button" class="choice" role="radio" aria-checked={choice === 'backup'} disabled={!!pending} onclick={() => (choice = 'backup')}>
			<span class="choice-icon" aria-hidden="true"><Download size={17} strokeWidth={1.75} /></span>
			<span class="copy"><span class="label">Private backup</span><span class="note">.jsonl · Raw history; may contain secrets. Do not share.</span></span>
			<Check size={18} strokeWidth={2.4} class="choice-check" aria-hidden="true" />
		</button>
	</div>
	<div class="page-foot">
		<button type="button" class="primary" disabled={!!pending || !sessionId} aria-busy={!!pending} onclick={() => download(choice)}>
			{#if pending}<LoaderCircle class="export-spinner" size={17} strokeWidth={2} aria-hidden="true" />{pending === 'sharing' ? 'Exporting…' : 'Downloading…'}{:else}{choice === 'sharing' ? 'Export for sharing' : 'Download private backup'}{/if}
		</button>
	</div>
</div>
{:else}
<div class="exports" class:menu aria-label="Chat exports">
	<button class="export-btn" disabled={!!pending || !sessionId} aria-busy={pending === 'backup'}
		aria-label="Download private backup (.jsonl)" title={privateNote} onclick={() => download('backup')}>
		{#if pending === 'backup'}<LoaderCircle class="export-spinner" size={16} strokeWidth={1.5} />{:else}<Download size={16} strokeWidth={1.5} />{/if}
		{#if menu}<span class="copy"><span class="label">{pending === 'backup' ? 'Downloading private backup…' : 'Download private backup'}</span><span class="note">.jsonl · Raw history; may contain secrets. Do not share.</span></span>{/if}
	</button>
	<button class="export-btn" disabled={!!pending || !sessionId} aria-busy={pending === 'sharing'}
		aria-label="Export for sharing (.md)" title={sharingNote} onclick={() => download('sharing')}>
		{#if pending === 'sharing'}<LoaderCircle class="export-spinner" size={16} strokeWidth={1.5} />{:else}<FileDown size={16} strokeWidth={1.5} />{/if}
		{#if menu}<span class="copy"><span class="label">{pending === 'sharing' ? 'Exporting for sharing…' : 'Export for sharing'}</span><span class="note">.md · Recognized secrets removed; review before sharing.</span></span>{/if}
	</button>
</div>
{/if}

<style>
	.exports { display: flex; align-items: center; gap: 2px; }
	.export-btn { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; flex-shrink: 0; padding: 0; background: transparent; border: none; border-radius: calc(var(--radius, 0.75rem) * 0.5); color: var(--muted-foreground); cursor: pointer; transition: background-color 150ms ease, color 150ms ease; -webkit-tap-highlight-color: transparent; }
	.export-btn:hover:not(:disabled) { background: var(--hover-overlay); color: var(--foreground); }
	.export-btn:active:not(:disabled) { background: var(--hover-overlay); transform: translateY(1px); }
	.export-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.export-btn:disabled { opacity: 0.5; cursor: default; }
	.menu { flex-direction: column; align-items: stretch; gap: 0; }
	.menu .export-btn { width: 100%; height: auto; min-height: 44px; justify-content: flex-start; gap: 9px; padding: 9px 8px; text-align: left; }
	.copy { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
	.label { color: var(--foreground); font-size: 12px; font-weight: 500; line-height: 1.35; }
	.note { font-size: 11px; line-height: 1.5; }
	.exports :global(.export-spinner) { animation: spin 1s linear infinite; }
	/* Page layout: grouped rows over a primary button pinned to the bottom. */
	.page { flex: 1; min-height: 0; flex-direction: column; align-items: stretch; gap: 0; }
	.choices { flex: 1 1 auto; min-height: 0; overflow-y: auto; align-self: stretch; margin: 2px 14px 0; border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) * 1.15); background: var(--card); }
	.page .choices { flex: 0 1 auto; }
	.choice { display: flex; align-items: center; gap: 12px; width: 100%; min-height: 60px; padding: 10px 14px; border: 0; background: none; color: var(--foreground); font: inherit; text-align: left; cursor: pointer; -webkit-tap-highlight-color: transparent; }
	.choice + .choice { border-top: 1px solid var(--border); }
	.choice:active:not(:disabled) { background: var(--muted); }
	.choice:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; border-radius: inherit; }
	.choice:disabled { cursor: default; }
	.choice-icon { display: grid; place-items: center; width: 30px; height: 30px; flex: none; border-radius: calc(var(--radius, 0.75rem) * 0.65); background: var(--muted); color: var(--foreground); }
	.choice .copy { flex: 1; }
	.choice .label { font-size: 15px; font-weight: 500; }
	.choice .note { color: var(--muted-foreground); font-size: 12.5px; line-height: 1.35; }
	.choice[aria-checked='true'] .label { font-weight: 600; }
	.choice :global(.choice-check) { flex: none; color: var(--primary); visibility: hidden; }
	.choice[aria-checked='true'] :global(.choice-check) { visibility: visible; }
	.page-foot { flex: none; margin-top: auto; padding: 10px 14px calc(14px + env(safe-area-inset-bottom, 0px)); border-top: 1px solid var(--border); background: var(--popover); }
	.primary { display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%; height: 46px; border: 0; border-radius: calc(var(--radius, 0.75rem) * 1); background: var(--foreground); color: var(--background); font: inherit; font-size: 15px; font-weight: 600; cursor: pointer; transition: opacity 150ms ease, transform 150ms ease; }
	.primary:active:not(:disabled) { transform: scale(0.985); }
	.primary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.primary:disabled { opacity: 0.5; cursor: default; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (max-width: 639px) { .export-btn { width: 44px; height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .export-btn, .primary { transition: none; } .exports :global(.export-spinner) { animation: none; } }
</style>
