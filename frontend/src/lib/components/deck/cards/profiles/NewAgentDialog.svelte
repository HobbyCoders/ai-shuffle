<script lang="ts" module>
	export type NewAgentMode = 'blank' | 'builtin' | 'describe' | 'import';
</script>

<script lang="ts">
	/**
	 * NewAgentDialog: the one way to start an agent. Blank, a copy of a
	 * built-in's prompt and tools (offered only when the worker has
	 * built-ins), an AI draft from a description (handed to
	 * the AI generate dialog, which can ask follow-up questions), or an
	 * exported .json. A bottom sheet on narrow screens.
	 */
	import { tick, untrack } from 'svelte';
	import { Copy, FileText, Sparkles, Upload, X } from 'lucide-svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';

	interface Props {
		open: boolean;
		builtins: { id: string; name: string }[];
		busy?: boolean;
		onClose: () => void;
		onBlank: () => void;
		onFromBuiltin: (id: string) => void;
		onDescribe: (text: string) => void;
		onImport: (file: File) => void;
	}

	let { open, builtins, busy = false, onClose, onBlank, onFromBuiltin, onDescribe, onImport }: Props = $props();

	const OPTIONS: { id: NewAgentMode; title: string; hint: string; icon: typeof FileText }[] = [
		{ id: 'blank', title: 'Blank', hint: 'Start empty.', icon: FileText },
		{ id: 'builtin', title: 'From a built-in', hint: 'Copy its prompt and tools.', icon: Copy },
		{ id: 'describe', title: 'Describe it', hint: 'AI drafts the agent.', icon: Sparkles },
		{ id: 'import', title: 'Import', hint: 'From an exported .json.', icon: Upload }
	];

	/** "From a built-in" only when there is one to copy. */
	const options = $derived(builtins.length ? OPTIONS : OPTIONS.filter((o) => o.id !== 'builtin'));

	let mode = $state<NewAgentMode>('blank');
	let builtin = $state('');
	let text = $state('');
	let file = $state<File | null>(null);
	let fileInput: HTMLInputElement | undefined = $state();
	let list: HTMLDivElement | undefined = $state();

	// Fresh choices every time it opens.
	$effect(() => {
		if (!open) return;
		untrack(() => {
			mode = 'blank';
			text = '';
			file = null;
			builtin = builtins[0]?.id ?? '';
		});
	});

	const ready = $derived(
		!busy && (mode === 'import' ? !!file : mode === 'builtin' ? !!builtin : true)
	);
	const action = $derived(
		mode === 'describe' ? 'Continue' : mode === 'import' ? 'Import' : 'Create'
	);

	function create() {
		if (!ready) return;
		if (mode === 'blank') onBlank();
		else if (mode === 'builtin') onFromBuiltin(builtin);
		else if (mode === 'describe') onDescribe(text.trim());
		else if (file) onImport(file);
	}

	async function pick(next: NewAgentMode) {
		mode = next;
		await tick();
		list?.querySelector<HTMLElement>(`[data-mode="${next}"]`)?.focus();
	}

	function optionKey(event: KeyboardEvent) {
		const keys = ['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft'];
		if (!keys.includes(event.key)) return;
		event.preventDefault();
		const i = options.findIndex((o) => o.id === mode);
		const step = event.key === 'ArrowDown' || event.key === 'ArrowRight' ? 1 : -1;
		void pick(options[(i + step + options.length) % options.length].id);
	}

	function onWindowKey(event: KeyboardEvent) {
		if (open && event.key === 'Escape' && !busy) {
			event.preventDefault();
			onClose();
		}
	}
</script>

<svelte:window onkeydown={onWindowKey} />

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div class="scrim" role="presentation" onclick={(e) => e.target === e.currentTarget && !busy && onClose()}>
		<div class="dlg" role="dialog" aria-modal="true" aria-labelledby="new-agent-title" tabindex="-1" use:focusTrap={{ initial: '[aria-checked="true"]' }}>
			<div class="dlg-h">
				<h2 id="new-agent-title">New agent</h2>
				<button type="button" class="st-btn ghost icon sm" aria-label="Close" onclick={onClose} disabled={busy}><X /></button>
			</div>
			<div class="dlg-b" role="radiogroup" aria-label="How to start" bind:this={list}>
				{#each options as opt (opt.id)}
					{@const on = mode === opt.id}
					<button
						type="button"
						class="opt"
						role="radio"
						data-mode={opt.id}
						aria-checked={on}
						tabindex={on ? 0 : -1}
						onclick={() => pick(opt.id)}
						onkeydown={optionKey}
					>
						<span class="ic"><opt.icon /></span>
						<span class="tx"><span class="t">{opt.title}</span><span class="h">{opt.hint}</span></span>
						<span class="rd" aria-hidden="true"></span>
					</button>
					{#if on && opt.id === 'builtin'}
						<div class="optx">
							<label class="lab" for="new-agent-builtin">Built-in</label>
							<select id="new-agent-builtin" class="sel" bind:value={builtin}>
								{#each builtins as b (b.id)}<option value={b.id}>{b.name}</option>{/each}
							</select>
						</div>
					{:else if on && opt.id === 'describe'}
						<div class="optx">
							<label class="lab" for="new-agent-text">What should it do?</label>
							<textarea id="new-agent-text" class="area" rows={3} placeholder="Turns merged PRs into release notes." bind:value={text}></textarea>
						</div>
					{:else if on && opt.id === 'import'}
						<div class="optx">
							<input
								bind:this={fileInput}
								type="file"
								accept=".json,application/json"
								class="st-vh"
								tabindex="-1"
								aria-hidden="true"
								onchange={(e) => (file = (e.target as HTMLInputElement).files?.[0] ?? null)}
							/>
							<div class="filerow">
								<button type="button" class="st-btn sm" onclick={() => fileInput?.click()}><Upload />Choose file</button>
								<span class="lab">{file ? file.name : 'No file chosen'}</span>
							</div>
						</div>
					{/if}
				{/each}
			</div>
			<div class="dlg-f">
				<button type="button" class="st-btn" onclick={onClose} disabled={busy}>Cancel</button>
				<button type="button" class="st-btn primary" disabled={!ready} onclick={create}>
					{#if mode === 'describe'}<Sparkles />{/if}{busy ? 'Working…' : action}
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.scrim {
		position: absolute;
		inset: 0;
		z-index: 60;
		display: grid;
		place-items: center;
		padding: 20px;
		background: color-mix(in oklch, var(--background) 62%, transparent);
		backdrop-filter: blur(3px);
		animation: nad-fade 0.15s ease-out;
	}
	.dlg {
		width: min(520px, 100%);
		max-height: 100%;
		overflow: auto;
		background: var(--st-popover);
		border-radius: calc(var(--st-radius) + 4px);
		box-shadow: var(--st-shadow);
		animation: nad-pop 0.18s ease-out;
	}
	.dlg:focus {
		outline: none;
	}
	.dlg-h {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		padding: 18px 18px 6px 20px;
	}
	h2 {
		font-size: 16px;
		font-weight: 600;
		margin: 0;
	}
	.dlg-b {
		padding: 8px 20px 4px;
		display: grid;
		gap: 8px;
	}
	.dlg-f {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 8px;
		padding: 14px 20px 18px;
	}
	.opt {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		gap: 12px;
		align-items: center;
		width: 100%;
		padding: 11px 12px;
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius);
		background: var(--st-card);
		text-align: left;
		color: inherit;
		transition:
			border-color 0.12s,
			background 0.12s;
	}
	.opt:hover {
		border-color: var(--st-border-strong);
	}
	.opt[aria-checked='true'] {
		border-color: var(--st-primary);
		box-shadow: 0 0 0 1px var(--st-primary);
	}
	.ic {
		width: 32px;
		height: 32px;
		border-radius: 9px;
		display: grid;
		place-items: center;
		background: var(--st-muted);
		color: var(--st-mfg);
	}
	.opt[aria-checked='true'] .ic {
		color: var(--st-primary);
		background: var(--st-primary-soft);
	}
	.tx {
		display: grid;
		min-width: 0;
	}
	.t {
		font-size: 13.5px;
		font-weight: 500;
	}
	.h {
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.rd {
		width: 16px;
		height: 16px;
		border-radius: 50%;
		border: 1.5px solid var(--st-border-strong);
	}
	.opt[aria-checked='true'] .rd {
		border: 5px solid var(--st-primary);
	}
	.optx {
		padding: 2px 2px 8px 56px;
		display: grid;
		gap: 6px;
	}
	.lab {
		font-size: 12px;
		color: var(--st-mfg);
	}
	.filerow {
		display: flex;
		gap: 10px;
		align-items: center;
		min-width: 0;
	}
	.sel,
	.area {
		width: 100%;
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		border-radius: var(--st-radius-md);
		font-size: 13px;
		color: var(--st-fg);
		outline: none;
	}
	.sel {
		height: 32px;
		padding: 0 8px;
	}
	.sel option {
		background: var(--st-popover);
		color: var(--st-fg);
	}
	.area {
		padding: 8px 10px;
		line-height: 1.5;
		resize: vertical;
	}
	.sel:focus,
	.area:focus {
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	@keyframes nad-fade {
		from {
			opacity: 0;
		}
	}
	@keyframes nad-pop {
		from {
			opacity: 0;
			transform: translateY(-4px);
		}
	}
	/* Phone and narrow panes: a bottom sheet. */
	@container (max-width: 600px) {
		.scrim {
			place-items: end stretch;
			padding: 0;
		}
		.dlg {
			width: 100%;
			border-radius: 18px 18px 0 0;
			max-height: 92%;
		}
		.dlg-f .st-btn {
			height: 40px;
			flex: 1;
			justify-content: center;
		}
		.optx {
			padding-left: 2px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.scrim,
		.dlg {
			animation: none;
		}
		.opt {
			transition: none;
		}
	}
</style>
