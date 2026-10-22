<script lang="ts">
	/**
	 * AttachProjectSheet — give a running no-project session its project
	 * (#371 step 4). A centred sheet on desktop, a bottom sheet on the phone.
	 *
	 * Lists the projects this worker has a checkout of (others show which
	 * worker has them, greyed), says what appears once attached, and states
	 * plainly that it is one-way before the confirm. The backend holds the same
	 * rule (POST /sessions/{id}/attach-project refuses a second attach).
	 */
	import { FolderClosed, GitBranch, Lock, ShieldCheck, SquareTerminal, X, Check, Loader2 } from 'lucide-svelte';
	import { clusterProjects, projects } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { attachProject } from '$lib/api/sessions';
	import { getErrorMessage } from '$lib/api/client';
	import { notify } from '$lib/stores/notifications';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import { buildWorkspacePickerItems } from './workspace-catalog';
	import { ATTACH_GAINS, attachWarning } from '$lib/components/chat/session-scope';

	interface Props {
		open: boolean;
		sessionId: string;
		agentName: string | null;
		/** Why attaching is not possible right now (busy, another worker), or null. */
		blockedReason: string | null;
		onClose: () => void;
	}

	let { open, sessionId, agentName, blockedReason, onClose }: Props = $props();

	let picked = $state<string | null>(null);
	let attaching = $state(false);
	let error = $state<string | null>(null);

	const items = $derived(buildWorkspacePickerItems($projects, $clusterProjects));
	const pickedItem = $derived(items.find((i) => i.id === picked) ?? null);
	const warning = $derived(attachWarning(agentName, pickedItem?.name));
	const canAttach = $derived(!!pickedItem && !pickedItem.remoteOnly && !blockedReason && !attaching);

	$effect(() => {
		if (!open) { picked = null; error = null; attaching = false; }
	});

	// Radiogroup keyboard model: one stop in the Tab order (the picked row, else
	// the first one this worker can attach), and the arrow keys move the pick
	// and the focus together. Rows on another worker can't be picked, so the
	// arrows skip them; they stay listed with their reason.
	let rowRefs = $state<Record<string, HTMLButtonElement | null>>({});
	const pickable = $derived(items.filter((i) => !i.remoteOnly));
	const tabStop = $derived(picked ?? pickable[0]?.id ?? null);

	function onRadioKeydown(e: KeyboardEvent, id: string) {
		if (!pickable.length) return;
		const at = pickable.findIndex((i) => i.id === id);
		let next: number;
		switch (e.key) {
			case 'ArrowDown':
			case 'ArrowRight':
				next = (at + 1) % pickable.length;
				break;
			case 'ArrowUp':
			case 'ArrowLeft':
				next = (at - 1 + pickable.length) % pickable.length;
				break;
			case 'Home':
				next = 0;
				break;
			case 'End':
				next = pickable.length - 1;
				break;
			default:
				return;
		}
		e.preventDefault();
		if (attaching) return;
		picked = pickable[next].id;
		rowRefs[picked]?.focus();
	}

	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return { destroy() { node.remove(); } };
	}

	function onKeydown(e: KeyboardEvent) {
		if (open && e.key === 'Escape' && !attaching) {
			e.stopPropagation();
			onClose();
		}
	}

	async function confirm() {
		if (!canAttach || !pickedItem) return;
		attaching = true;
		error = null;
		try {
			const result = await attachProject(sessionId, pickedItem.project);
			activeSession.applyAttachedProject(sessionId, result.project_id, result.project_v2_id, result.permission_mode ?? 'default');
			notify(`${pickedItem.name} attached. ${agentName?.trim() || 'Your agent'} now works in its checkout, on Ask.`, { severity: 'success' });
			onClose();
		} catch (e) {
			error = getErrorMessage(e, 'Could not attach the project.');
		} finally {
			attaching = false;
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#if open}
	<div class="attach-layer" use:portal>
		<button type="button" class="attach-scrim" aria-label="Close" tabindex="-1" onclick={() => !attaching && onClose()}></button>
		<div class="attach-sheet" role="dialog" aria-modal="true" aria-labelledby="attach-title" tabindex="-1" use:focusTrap>
			<div class="grab" aria-hidden="true"></div>
			<header class="sheet-head">
				<div>
					<h2 id="attach-title">Attach a project</h2>
					<p class="sub">{agentName?.trim() || 'Your agent'} keeps this chat and gains a checkout.</p>
				</div>
				<button type="button" class="icon-btn" onclick={onClose} aria-label="Close" disabled={attaching}>
					<X size={16} strokeWidth={2} aria-hidden="true" />
				</button>
			</header>

			<div class="proj-list" role="radiogroup" aria-label="Project">
				{#each items as item (item.id)}
					<button
						bind:this={rowRefs[item.id]}
						type="button"
						class="proj-row"
						class:selected={picked === item.id}
						role="radio"
						aria-checked={picked === item.id}
						aria-disabled={item.remoteOnly ? 'true' : undefined}
						tabindex={item.id === tabStop ? 0 : -1}
						title={item.remoteOnly ? 'Not checked out on this worker' : item.name}
						onclick={() => { if (!item.remoteOnly && !attaching) picked = item.id; }}
						onkeydown={(e) => onRadioKeydown(e, item.id)}
					>
						<FolderClosed size={15} strokeWidth={1.7} aria-hidden="true" />
						<span class="proj-name">{item.name}</span>
						{#if item.remoteOnly}
							<span class="proj-note">on another worker</span>
						{:else if picked === item.id}
							<Check size={14} strokeWidth={2.4} class="proj-check" aria-hidden="true" />
						{/if}
					</button>
				{:else}
					<p class="empty">No projects yet. Add one from the Projects page.</p>
				{/each}
			</div>

			<div class="checkout-row">
				<span class="row-lbl">Checkout</span>
				<span class="row-val">Main checkout</span>
			</div>

			<div class="gains" aria-label="Appears after attaching">
				<span class="row-lbl">Appears after attaching</span>
				<ul>
					{#each ATTACH_GAINS as gain, i (gain)}
						<li style:--d="{i * 40}ms">
							{#if i === 0 || i === 1}<GitBranch size={13} strokeWidth={1.8} aria-hidden="true" />{:else if i === 3}<SquareTerminal size={13} strokeWidth={1.8} aria-hidden="true" />{:else}<FolderClosed size={13} strokeWidth={1.8} aria-hidden="true" />{/if}
							{gain}
						</li>
					{/each}
				</ul>
			</div>

			<div class="one-way" role="note">
				<Lock size={15} strokeWidth={2} aria-hidden="true" />
				<div>
					<strong>{warning.title}</strong>
					<p>{warning.body}</p>
					<p class="access-note"><ShieldCheck size={13} strokeWidth={2} aria-hidden="true" />{warning.access}</p>
				</div>
			</div>

			{#if blockedReason}
				<p class="blocked" role="status">{blockedReason}</p>
			{/if}
			{#if error}
				<p class="error" role="alert">{error}</p>
			{/if}

			<footer class="sheet-foot">
				<button type="button" class="btn ghost" onclick={onClose} disabled={attaching}>Cancel</button>
				<button type="button" class="btn primary" onclick={confirm} disabled={!canAttach}>
					{#if attaching}<Loader2 size={14} strokeWidth={2} class="spin" aria-hidden="true" />{/if}
					{pickedItem ? `Attach ${pickedItem.name}` : 'Attach'}
				</button>
			</footer>
		</div>
	</div>
{/if}

<style>
	.attach-layer { position: fixed; inset: 0; z-index: 200; display: grid; place-items: center; padding: 16px; }
	.attach-scrim {
		position: absolute;
		inset: 0;
		border: none;
		background: var(--modal-overlay, color-mix(in oklch, var(--background) 60%, transparent));
		animation: fade-in 200ms ease both;
		cursor: default;
	}
	.attach-sheet {
		position: relative;
		width: min(520px, 100%);
		max-height: min(640px, calc(100dvh - 32px));
		overflow-y: auto;
		padding: 18px 18px 16px;
		background: var(--popover);
		color: var(--foreground);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 10px) * 1.4);
		box-shadow: var(--shadow-l);
		animation: sheet-in 260ms cubic-bezier(0.16, 1, 0.3, 1) both;
	}
	.attach-sheet:focus { outline: none; }
	.grab { display: none; }
	.sheet-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
	h2 { font-family: var(--font-display, Georgia, serif); font-size: 1.375rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.2; }
	.sub { margin-top: 3px; font-size: 13px; color: var(--muted-foreground); }
	.icon-btn { display: grid; place-items: center; width: 30px; height: 30px; border: none; border-radius: 8px; background: transparent; color: var(--subtle-foreground); cursor: pointer; }
	.icon-btn:hover { background: var(--hover-overlay); color: var(--foreground); }
	.icon-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.proj-list { display: flex; flex-direction: column; gap: 2px; max-height: 220px; overflow-y: auto; padding: 4px; margin: 0 -4px 12px; }
	.proj-row {
		display: flex; align-items: center; gap: 10px; min-height: 40px; padding: 0 10px;
		border: 1px solid transparent; border-radius: calc(var(--radius, 10px) * 0.8);
		background: transparent; color: var(--muted-foreground); font: inherit; font-size: 13.5px; text-align: left; cursor: pointer;
		transition: background 150ms ease, color 150ms ease, border-color 150ms ease;
	}
	.proj-row:hover:not([aria-disabled='true']) { background: var(--hover-overlay); color: var(--foreground); }
	.proj-row:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.proj-row.selected { color: var(--foreground); background: var(--selection-bg, color-mix(in oklch, var(--primary) 10%, transparent)); border-color: color-mix(in oklch, var(--primary) 40%, var(--border)); }
	.proj-row[aria-disabled='true'] { opacity: 0.5; cursor: not-allowed; }
	.proj-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
	.proj-note { font-size: 11.5px; color: var(--subtle-foreground); }
	.proj-row :global(.proj-check) { color: var(--primary); }
	.empty { padding: 10px; font-size: 13px; color: var(--muted-foreground); }

	.row-lbl { font-size: 11.5px; font-weight: 500; color: var(--subtle-foreground); }
	.checkout-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; margin-bottom: 10px; border-radius: calc(var(--radius, 10px) * 0.8); background: var(--muted); }
	.row-val { font-size: 13px; font-weight: 500; color: var(--foreground); }

	.gains { padding: 2px 2px 12px; }
	.gains ul { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
	.gains li {
		display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 9px;
		border: 1px dashed var(--border-strong); border-radius: 999px;
		font-size: 12px; color: var(--muted-foreground);
		animation: chip-in 300ms cubic-bezier(0.16, 1, 0.3, 1) both; animation-delay: var(--d);
	}

	.one-way {
		display: flex; gap: 10px; padding: 12px; margin-bottom: 12px;
		border: 1px solid var(--status-warning-border, color-mix(in oklch, var(--warning) 28%, transparent));
		background: var(--status-warning-bg, color-mix(in oklch, var(--warning) 12%, transparent));
		border-radius: calc(var(--radius, 10px) * 0.9);
		color: var(--warning);
	}
	.one-way :global(svg) { flex: none; margin-top: 2px; }
	.one-way strong { display: block; font-size: 13px; font-weight: 600; color: var(--foreground); }
	.one-way p { margin-top: 2px; font-size: 12.5px; line-height: 1.45; color: var(--muted-foreground); }
	.one-way .access-note { display: flex; align-items: flex-start; gap: 6px; margin-top: 6px; color: var(--foreground); font-weight: 500; }
	.one-way .access-note :global(svg) { margin-top: 2px; color: var(--muted-foreground); }
	.blocked, .error { margin-bottom: 10px; font-size: 12.5px; }
	.blocked { color: var(--muted-foreground); }
	.error { color: var(--destructive); }

	.sheet-foot { display: flex; justify-content: flex-end; gap: 8px; }
	.btn { display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 14px; border-radius: calc(var(--radius, 10px) * 0.8); font: inherit; font-size: 13px; font-weight: 550; cursor: pointer; border: 1px solid transparent; transition: background 150ms ease, opacity 150ms ease; }
	.btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.btn.ghost { background: transparent; border-color: var(--border); color: var(--foreground); }
	.btn.ghost:hover:not(:disabled) { background: var(--hover-overlay); }
	.btn.primary { background: var(--primary); color: var(--primary-foreground); }
	.btn.primary:hover:not(:disabled) { background: color-mix(in oklch, var(--primary) 88%, var(--foreground)); }
	.btn:disabled { opacity: 0.45; cursor: not-allowed; }
	.btn :global(.spin) { animation: spin 0.9s linear infinite; }

	@keyframes fade-in { from { opacity: 0; } }
	@keyframes sheet-in { from { opacity: 0; transform: translateY(10px) scale(0.98); } }
	@keyframes chip-in { from { opacity: 0; transform: translateY(4px); } }
	@keyframes spin { to { transform: rotate(360deg); } }

	@media (prefers-reduced-motion: reduce) {
		.attach-scrim, .attach-sheet, .gains li { animation: none; }
		.proj-row, .btn { transition: none; }
	}
</style>
