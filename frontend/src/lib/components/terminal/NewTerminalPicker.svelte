<script lang="ts">
	/** Folder and shell for a new terminal. Used as the "+" popover and as the empty state. */
	import { tick } from 'svelte';
	import { Folder, TerminalSquare, ShieldCheck, LoaderCircle, TriangleAlert } from 'lucide-svelte';
	import { cwdProblem, type ProbeState } from '$lib/terminal/gating';
	import type { CreateTerminal, TerminalTarget } from '$lib/api/terminals';
	import { APPROVAL_REQUIRED, PENDING_SECONDS, isPeerTarget, localWorkerId, openErrorMessage, peerChip, peerReason, selectable, type OpenResult } from '$lib/terminal/peer';
	import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
	import PeerCode from './PeerCode.svelte';

	let {
		probe,
		busy = false,
		disabledReason = null,
		inline = false,
		blurb = 'A shell for this chat. It keeps running when you close the panel.',
		onOpen,
		onCancel,
		onTargetsChanged,
	}: {
		probe: Extract<ProbeState, { status: 'ready' }>;
		busy?: boolean;
		/** The cap is reached: say why instead of opening. */
		disabledReason?: string | null;
		inline?: boolean;
		/** The empty state's line under the title. */
		blurb?: string;
		onOpen: (choice: CreateTerminal & { cwd: string | null; shell: string | null }) => void | Promise<OpenResult | void>;
		onCancel?: () => void;
		/** Ask for fresh targets (after a revoke), so the rows show what the worker now reports. */
		onTargetsChanged?: () => void;
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
	const targetKey = (target: TerminalTarget) => `${target.kind}:${target.worker_id ?? target.grant_id}`;
	// Reopening the popover returns to a peer whose request is still in flight.
	// svelte-ignore state_referenced_locally
	let selected = $state((() => {
		const local = localWorkerId(probe.targets);
		const waiting = (probe.targets ?? []).find((target, index) => index > 0 && isPeerTarget(target, local)
			&& !!peerTerminals.flow(target.worker_id) && peerTerminals.flow(target.worker_id)?.phase !== 'ended');
		return waiting ? targetKey(waiting) : '';
	})());
	// A lease revoked here stays out of the row until the targets are probed again.
	const masked = (target: TerminalTarget): TerminalTarget => target.authorization_id && peerTerminals.isRevoked(target.authorization_id)
		? { ...target, available: false, authorization_id: undefined, reason_code: APPROVAL_REQUIRED, reason: null } : target;
	const destinations = $derived((probe.targets ?? []).map(masked));
	const destination = $derived(selected ? destinations.find((target) => targetKey(target) === selected) : destinations[0]);
	const remote = $derived(!!selected);
	const unavailable = $derived(destination?.available === false || (remote && !destination));
	const ssh = $derived(destination?.kind === 'ssh');
	let input = $state<HTMLInputElement>();
	/** The peer refused the folder (`bad_folder`). The approval is kept: fix it and open again. */
	let remoteFolder = $state<{ folder: string; message: string } | null>(null);
	const folderError = $derived(folder.trim() ? cwdProblem(folder.trim()) ?? '' : '');
	const remoteFolderError = $derived(remoteFolder && remoteFolder.folder === folder ? remoteFolder.message : '');

	// ── Peer workers (#196): only when the worker lists one ────────────────
	const localId = $derived(localWorkerId(destinations));
	const peers = $derived(destinations.some((target) => isPeerTarget(target, localId)));
	const peer = $derived(isPeerTarget(destination, localId) ? destination : undefined);
	const flow = $derived(peer ? peerTerminals.flow(peer.worker_id) : null);
	/** The approval this picker would open with: the live flow's, else one the worker reports for this owner. */
	const authorizationId = $derived(flow?.phase === 'approved' ? flow.authorizationId
		: !flow && peer?.available ? peer.authorization_id : undefined);
	const stage = $derived.by(() => {
		if (!peer) return null;
		if (flow?.phase === 'requesting' || flow?.phase === 'pending' || flow?.phase === 'ended') return flow.phase;
		if (authorizationId) return 'approved';
		return selectable(peer) ? 'request' : 'blocked';
	});
	const left = (expiresAt: number) => Math.max(0, (expiresAt - peerTerminals.now) / 1000);
	let opening = $state(false);
	const peerKey = (target: TerminalTarget) => `peer-reason-${(target.worker_id ?? target.grant_id ?? '').replace(/[^a-z0-9_-]/gi, '_')}`;
	const kindLabel = (target: TerminalTarget, index: number) =>
		index === 0 ? 'This worker' : target.kind === 'ssh' ? 'SSH grant' : 'Peer worker';

	$effect(() => { if (!inline) input?.focus(); });

	function pick(value: string) {
		selected = value;
		folder = selected ? '' : probe.folder ?? '';
	}

	function requestApproval() {
		if (!peer?.worker_id || busy || disabledReason) return;
		void peerTerminals.request(peer.worker_id, peer.name);
	}

	async function openPeer() {
		if (!peer?.worker_id || !authorizationId || opening) return;
		const workerId = peer.worker_id;
		const name = peer.name;
		opening = true;
		try {
			const tried = folder;
			const result = await onOpen({ cwd: folder.trim() || null, shell: null,
				target: { kind: 'worker', worker_id: workerId, authorization_id: authorizationId } });
			remoteFolder = null;
			if (result && !result.ok && result.code === 'bad_folder') {
				// Nothing was spent on either worker: correct the folder and open again.
				remoteFolder = { folder: tried, message: `${name}: ${result.message || "that folder doesn't exist."}` };
				await tick();
				input?.focus();
			} else if (result && !result.ok) peerTerminals.fail(workerId, name, `The shell on ${name} didn't open`, openErrorMessage(result.code, result.message, name));
			else peerTerminals.clear(workerId);
		} finally {
			opening = false;
		}
	}

	async function revoke() {
		if (!peer?.worker_id) return;
		await peerTerminals.cancel(peer.worker_id, authorizationId);
		onTargetsChanged?.();
	}

	function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy || disabledReason || folderError) return;
		if (peer) {
			if (stage === 'approved') void openPeer();
			else if (stage === 'request' || (stage === 'ended' && flow?.phase === 'ended' && flow.retry && selectable(peer))) requestApproval();
			return;
		}
		if (unavailable) return;
		const cwd = folder.trim();
		onOpen({ cwd: ssh ? null : cwd && (remote || cwd !== probe.folder) ? cwd : null,
			shell: remote ? null : shell || null,
			...(destination ? { target: { kind: destination.kind, worker_id: destination.worker_id, grant_id: destination.grant_id } } : {}) });
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<form class="picker" class:inline class:wide={peers} onsubmit={submit} aria-label="New terminal"
	onkeydown={(e) => { if (e.key === 'Escape' && onCancel) { e.preventDefault(); e.stopPropagation(); onCancel(); } }}>
	{#if inline}
		<div class="lede">
			<span class="glyph" aria-hidden="true"><TerminalSquare size={20} /></span>
			<div>
				<h3>Open a terminal on {destination?.name ?? probe.worker}</h3>
				<p>{blurb}</p>
			</div>
		</div>
	{/if}
	{#if peers}
		<fieldset class="field">
			<legend class="label">Target</legend>
			<div class="targets">
				{#each destinations as target, index (targetKey(target))}
					{@const value = index === 0 ? '' : targetKey(target)}
					{@const chip = index === 0 || target.kind === 'ssh' ? (target.available ? null : { label: 'Unavailable', tone: 'off' }) : peerChip(target)}
					{@const reason = isPeerTarget(target, localId) ? peerReason(target) : null}
					<label class="target" class:on={selected === value} class:off={!selectable(target)}>
						<input type="radio" name="terminal-target" {value} checked={selected === value} disabled={!selectable(target)}
							aria-label={[target.name, kindLabel(target, index), chip?.label].filter(Boolean).join(', ')}
							aria-describedby={reason ? peerKey(target) : undefined} onchange={() => pick(value)} />
						<span class="t-text">
							<span class="t-name">{target.name}</span>
							<span class="t-kind">{kindLabel(target, index)}</span>
						</span>
						{#if chip}<span class="chip" data-tone={chip.tone}>{chip.label}</span>{/if}
						{#if reason}<span class="t-reason" id={peerKey(target)}>{reason}</span>{/if}
					</label>
				{/each}
			</div>
		</fieldset>
	{:else if destinations.length > 1}
		<label class="field">
			<span class="label">Target</span>
			<select bind:value={selected} onchange={() => { folder = selected ? '' : probe.folder ?? ''; }}>
				{#each destinations as target, index}
					<option value={index === 0 ? '' : targetKey(target)} disabled={!target.available}>{target.name}{target.kind === 'ssh' ? ' (SSH grant)' : ''}</option>
				{/each}
			</select>
		</label>
	{/if}
	{#if peer}
		{#if stage === 'request' || stage === 'requesting'}
			<p class="note peer-note">{peer.name} must approve this shell on {peer.name} itself. You'll both see the same six-character code to compare first.</p>
		{:else if stage === 'pending' && flow?.phase === 'pending'}
			<div class="pending" role="status" aria-live="polite">
				<p class="p-title"><LoaderCircle size={14} class="p-spin" aria-hidden="true" />Waiting for approval on {flow.name}</p>
				<PeerCode code={flow.code} left={left(flow.expiresAt)} total={PENDING_SECONDS} label={`Code for ${flow.name}`} />
				<p class="note">On <strong>{flow.name}</strong>, open Terminal and approve the request only if it shows this same code.</p>
				{#if flow.unreachable}<p class="note warn">Can't reach {flow.name} right now. Still waiting…</p>{/if}
			</div>
		{:else if stage === 'approved'}
			<p class="approved" role="status"><ShieldCheck size={15} aria-hidden="true" /><span>Approved on {peer.name} for one shell{#if flow?.phase === 'approved'} · <time>{Math.ceil(left(flow.expiresAt) / 60)} min</time> left{/if}</span></p>
			<label class="field">
				<span class="label">Folder</span>
				<span class="input-wrap" class:invalid={!!folderError || !!remoteFolderError}>
					<Folder size={14} aria-hidden="true" />
					<input bind:this={input} bind:value={folder} spellcheck="false" autocomplete="off" autocapitalize="off"
						placeholder={`Default folder on ${peer.name}`} aria-describedby="terminal-folder-note" aria-invalid={!!folderError || !!remoteFolderError} />
				</span>
				<span class="note" id="terminal-folder-note" role={remoteFolderError ? 'alert' : undefined}>{#if folderError}{folderError}{:else if remoteFolderError}{remoteFolderError} Fix the folder, or leave it empty for {peer.name}'s default; the approval still stands.{:else}A full path on {peer.name}, or empty for its default.{/if}</span>
			</label>
		{:else if stage === 'ended' && flow?.phase === 'ended'}
			<div class="ended" role="alert">
				<p class="p-title"><TriangleAlert size={14} aria-hidden="true" />{flow.title}</p>
				<p class="note">{flow.message}</p>
			</div>
		{/if}
	{:else if ssh}<p class="note">Uses this chat's approved SSH grant, login shell and home folder. Revoking the grant closes the terminal.</p>{:else}
	<label class="field">
		<span class="label">Folder</span>
		<span class="input-wrap" class:invalid={!!folderError}>
			<Folder size={14} aria-hidden="true" />
			<input bind:this={input} bind:value={folder} spellcheck="false" autocomplete="off" autocapitalize="off"
				placeholder="/workspace/my-project" aria-describedby="terminal-folder-note" aria-invalid={!!folderError} />
		</span>
		<span class="note" id="terminal-folder-note">
			{#if folderError}{folderError}{:else if probe.folderSource && folder.trim() === probe.folder}{SOURCE[probe.folderSource] ?? 'Default folder'}{:else}On {destination?.name ?? probe.worker}{/if}
		</span>
	</label>
	{/if}
	{#if !remote && probe.shells.length > 1}
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
		{#if onCancel}<button type="button" class="ghost" onclick={onCancel}>{stage === 'pending' ? 'Hide' : 'Cancel'}</button>{/if}
		{#if peer}
			{#if stage === 'pending' || stage === 'approved'}
				<button type="button" class="ghost danger" onclick={revoke}
					disabled={opening} aria-label={stage === 'pending' ? `Cancel the request to ${peer.name}` : `Revoke the approval on ${peer.name}`}>
					{stage === 'pending' ? 'Cancel request' : 'Revoke'}
				</button>
			{/if}
			{#if stage === 'approved'}
				<button type="submit" class="go" disabled={busy || opening || !!disabledReason || !!folderError || !!remoteFolderError}>{busy || opening ? 'Opening…' : 'Open terminal'}</button>
			{:else if stage !== 'pending'}
				<button type="submit" class="go" disabled={busy || !!disabledReason || stage === 'requesting' || stage === 'blocked' || (flow?.phase === 'ended' && (!flow.retry || !selectable(peer)))}>
					{stage === 'requesting' ? 'Requesting…' : stage === 'ended' ? 'Request again' : 'Request approval'}
				</button>
			{/if}
		{:else}
		<button type="submit" class="go" disabled={busy || !!disabledReason || !!folderError || unavailable}>
			{busy ? 'Opening…' : 'Open terminal'}
		</button>
		{/if}
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
	select { min-width: 0; width: 100%; min-height: 34px; padding: 0 8px; border: 1px solid var(--input); border-radius: calc(var(--radius) - 4px); background: var(--code-bg); color: var(--foreground); }
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
	/* ── Peer targets (#196) ── */
	.picker.wide { width: min(372px, calc(100vw - 32px)); }
	.picker.inline.wide { width: min(400px, 100%); }
	.targets { display: grid; gap: 4px; }
	.target { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 2px 10px;
		padding: 8px 10px 8px 12px; border: 1px solid var(--border); border-radius: calc(var(--radius) - 4px); background: var(--code-bg);
		cursor: pointer; transition: border-color 0.15s ease-out, background-color 0.15s ease-out; }
	.target:hover:not(.off) { border-color: var(--border-strong); background: color-mix(in oklch, var(--hover-overlay) 60%, var(--code-bg)); }
	.target.on { border-color: color-mix(in oklch, var(--primary) 60%, transparent);
		background: color-mix(in oklch, var(--primary) 8%, var(--code-bg)); box-shadow: inset 3px 0 0 var(--primary); }
	.target.off { cursor: not-allowed; }
	.target.off .t-name { color: var(--muted-foreground); }
	.target input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: inherit; }
	.target:has(input:focus-visible) { outline: 2px solid var(--ring); outline-offset: 1px; }
	.t-text { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
	.t-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 600 12.5px var(--font-sans); color: var(--foreground); }
	.t-kind { flex-shrink: 0; font-size: 11px; color: var(--subtle-foreground); }
	.t-reason { grid-column: 1 / -1; font-size: 11.5px; line-height: 1.4; color: var(--muted-foreground); }
	.chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 7px; border-radius: 999px; white-space: nowrap;
		font: 600 10.5px/1.4 var(--font-sans); letter-spacing: 0.02em; color: var(--muted-foreground); background: var(--muted); }
	.chip::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
	.chip[data-tone='ok'] { color: var(--success); background: color-mix(in oklch, var(--success) 13%, transparent); }
	.chip[data-tone='wait'] { color: var(--primary); background: color-mix(in oklch, var(--primary) 13%, transparent); }
	.chip[data-tone='warn'] { color: var(--warning); background: color-mix(in oklch, var(--warning) 13%, transparent); }
	.chip[data-tone='off']::before { background: var(--subtle-foreground); }
	.peer-note { margin: 0; line-height: 1.45; }
	.pending, .ended { display: grid; gap: 10px; padding: 12px; border-radius: calc(var(--radius) - 4px);
		border: 1px dashed color-mix(in oklch, var(--primary) 50%, transparent); background: color-mix(in oklch, var(--primary) 5%, transparent);
		animation: drop 0.2s cubic-bezier(0.2, 0.8, 0.2, 1); }
	.ended { border-style: solid; border-color: color-mix(in oklch, var(--warning) 40%, var(--border)); background: color-mix(in oklch, var(--warning) 8%, transparent); }
	.pending p, .ended p { margin: 0; line-height: 1.45; }
	.p-title { display: flex; align-items: center; gap: 7px; font: 600 12.5px var(--font-sans); color: var(--foreground); }
	.ended .p-title { color: var(--warning); }
	.pending :global(.p-spin) { color: var(--primary); animation: p-spin 1s linear infinite; }
	.note strong { color: var(--foreground); font-weight: 600; }
	.note.warn { color: var(--warning); }
	.approved { display: flex; align-items: center; gap: 8px; margin: 0; padding: 8px 10px; border-radius: calc(var(--radius) - 4px);
		font-size: 12px; color: var(--success); background: color-mix(in oklch, var(--success) 10%, transparent); }
	.approved time { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
	.ghost.danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 45%, var(--border-strong)); }
	.ghost.danger:hover:not(:disabled) { background: color-mix(in oklch, var(--destructive) 10%, transparent); }
	.ghost:disabled { opacity: 0.45; cursor: not-allowed; }
	@keyframes p-spin { to { transform: rotate(360deg); } }
	@keyframes drop { from { opacity: 0; transform: translateY(-4px) scale(0.98); } to { opacity: 1; transform: none; } }
	@media (max-width: 700px) { button, .input-wrap { min-height: 44px; } .seg-opt span { padding: 10px 18px; } .target { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .picker:not(.inline), .pending, .ended, .pending :global(.p-spin) { animation: none; } }
</style>
