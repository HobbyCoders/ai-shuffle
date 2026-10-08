<script lang="ts">
	/** Folder and shell for a new terminal. Used as the "+" popover and as the empty state. */
	import { Folder, TerminalSquare } from 'lucide-svelte';
	import { cwdProblem, type ProbeState } from '$lib/terminal/gating';

	let {
		probe,
		busy = false,
		disabledReason = null,
		inline = false,
		onOpen,
		onCancel,
	}: {
		probe: Extract<ProbeState, { status: 'ready' }>;
		busy?: boolean;
		/** The cap is reached: say why instead of opening. */
		disabledReason?: string | null;
		inline?: boolean;
		onOpen: (choice: { cwd: string | null; shell: string | null }) => void;
		onCancel?: () => void;
	} = $props();

	const SOURCE: Record<string, string> = {
		worktree: "This chat's worktree",
		project: "This chat's project",
		workspace: 'Workspace folder',
		home: 'Home folder',
	};

	// Seeded once from the probe; the owner edits from there.
	// svelte-ignore state_referenced_locally
	let folder = $state(probe.folder ?? '');
	// svelte-ignore state_referenced_locally
	let shell = $state(probe.defaultShell ?? probe.shells[0] ?? '');
	let input = $state<HTMLInputElement>();
	const folderError = $derived(folder.trim() ? cwdProblem(folder.trim()) ?? '' : '');

	$effect(() => { if (!inline) input?.focus(); });

	function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy || disabledReason || folderError) return;
		const cwd = folder.trim();
		onOpen({ cwd: cwd && cwd !== probe.folder ? cwd : null, shell: shell || null });
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<form class="picker" class:inline onsubmit={submit} aria-label="New terminal"
	onkeydown={(e) => { if (e.key === 'Escape' && onCancel) { e.preventDefault(); e.stopPropagation(); onCancel(); } }}>
	{#if inline}
		<div class="lede">
			<span class="glyph" aria-hidden="true"><TerminalSquare size={20} /></span>
			<div>
				<h3>Open a terminal on {probe.worker}</h3>
				<p>A shell for this chat. It keeps running when you close the panel.</p>
			</div>
		</div>
	{/if}
	<label class="field">
		<span class="label">Folder</span>
		<span class="input-wrap" class:invalid={!!folderError}>
			<Folder size={14} aria-hidden="true" />
			<input bind:this={input} bind:value={folder} spellcheck="false" autocomplete="off" autocapitalize="off"
				placeholder="/workspace/my-project" aria-describedby="terminal-folder-note" aria-invalid={!!folderError} />
		</span>
		<span class="note" id="terminal-folder-note">
			{#if folderError}{folderError}{:else if probe.folderSource && folder.trim() === probe.folder}{SOURCE[probe.folderSource] ?? 'Default folder'}{:else}On {probe.worker}{/if}
		</span>
	</label>
	{#if probe.shells.length > 1}
		<fieldset class="field">
			<legend class="label">Shell</legend>
			<div class="seg">
				{#each probe.shells as name (name)}
					<label class="seg-opt"><input type="radio" name="terminal-shell" value={name} bind:group={shell} /><span>{name}</span></label>
				{/each}
			</div>
		</fieldset>
	{/if}
	{#if disabledReason}<p class="cap" role="alert">{disabledReason}</p>{/if}
	<div class="actions">
		{#if onCancel}<button type="button" class="ghost" onclick={onCancel}>Cancel</button>{/if}
		<button type="submit" class="go" disabled={busy || !!disabledReason || !!folderError}>
			{busy ? 'Opening…' : 'Open terminal'}
		</button>
	</div>
</form>

<style>
	.picker { display: grid; gap: 12px; padding: 14px; width: min(320px, calc(100vw - 32px)); box-sizing: border-box; font-family: var(--font-sans); }
	.picker:not(.inline) {
		border: 1px solid var(--border-strong); border-radius: var(--radius); background: var(--popover);
		box-shadow: 0 16px 40px oklch(0 0 0 / 0.38); animation: drop 0.18s cubic-bezier(0.2, 0.8, 0.2, 1);
	}
	.picker.inline { width: min(360px, 100%); padding: 0; }
	.lede { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 4px; }
	.glyph { display: grid; place-items: center; width: 40px; height: 40px; flex-shrink: 0; border-radius: 10px; color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent); border: 1px solid color-mix(in oklch, var(--primary) 28%, transparent); }
	h3 { margin: 0; font-size: 14px; font-weight: 600; color: var(--foreground); }
	.lede p { margin: 3px 0 0; font-size: 12.5px; line-height: 1.45; color: var(--muted-foreground); }
	.field { display: grid; gap: 5px; margin: 0; padding: 0; border: 0; min-width: 0; }
	.label { padding: 0; font: 600 10.5px/1 var(--font-sans); letter-spacing: 0.06em; text-transform: uppercase; color: var(--subtle-foreground); }
	.input-wrap { display: flex; align-items: center; gap: 8px; padding: 0 10px; min-height: 34px; border: 1px solid var(--input);
		border-radius: calc(var(--radius) - 4px); background: var(--code-bg); color: var(--muted-foreground); transition: border-color 0.15s ease-out, box-shadow 0.15s ease-out; }
	.input-wrap:focus-within { border-color: var(--ring); box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 20%, transparent); }
	.input-wrap.invalid { border-color: var(--destructive); }
	input:not([type='radio']) { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--foreground); font: 12.5px var(--font-mono); }
	.note { font-size: 11.5px; color: var(--muted-foreground); }
	.invalid + .note, .input-wrap.invalid ~ .note { color: var(--destructive); }
	.seg { display: inline-flex; padding: 2px; gap: 2px; border-radius: calc(var(--radius) - 3px); background: var(--muted); width: fit-content; }
	.seg-opt { position: relative; }
	.seg-opt input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
	.seg-opt span { display: block; padding: 5px 14px; border-radius: calc(var(--radius) - 5px); font: 500 12px var(--font-mono); color: var(--muted-foreground); transition: background-color 0.15s, color 0.15s; }
	.seg-opt input:checked + span { background: var(--background); color: var(--foreground); box-shadow: 0 1px 2px oklch(0 0 0 / 0.25); }
	.seg-opt input:focus-visible + span { outline: 2px solid var(--ring); outline-offset: 1px; }
	.cap { margin: 0; padding: 8px 10px; border-radius: calc(var(--radius) - 4px); font-size: 12px; color: var(--warning);
		background: color-mix(in oklch, var(--warning) 11%, transparent); }
	.actions { display: flex; justify-content: flex-end; gap: 8px; }
	.inline .actions { justify-content: flex-start; }
	button { min-height: 32px; padding: 0 14px; border-radius: calc(var(--radius) - 4px); font: 500 12.5px var(--font-sans); cursor: pointer;
		transition: background-color 0.15s ease-out, opacity 0.15s; }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	button:active:not(:disabled) { transform: translateY(1px); }
	.ghost { border: 1px solid var(--border-strong); background: transparent; color: var(--foreground); }
	.ghost:hover { background: var(--hover-overlay); }
	.go { border: 1px solid var(--primary); background: var(--primary); color: var(--primary-foreground); }
	.go:hover:not(:disabled) { background: color-mix(in oklch, var(--primary) 88%, white); }
	.go:disabled { opacity: 0.45; cursor: not-allowed; }
	@keyframes drop { from { opacity: 0; transform: translateY(-4px) scale(0.98); } to { opacity: 1; transform: none; } }
	@media (max-width: 700px) { button, .input-wrap { min-height: 44px; } .seg-opt span { padding: 10px 18px; } }
	@media (prefers-reduced-motion: reduce) { .picker:not(.inline) { animation: none; } }
</style>
