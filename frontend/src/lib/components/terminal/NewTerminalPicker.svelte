<script lang="ts">
	/** Folder and shell for a new terminal. Used as the "+" popover and as the empty state. */
	import { tick } from 'svelte';
	import { Folder, TerminalSquare, TriangleAlert } from 'lucide-svelte';
	import { cwdProblem, type ProbeState } from '$lib/terminal/gating';
	import type { CreateTerminal, TerminalTarget } from '$lib/api/terminals';
	import { ACTION_OPEN, isPeerTarget, localWorkerId, openErrorMessage, peerRow, selectable, type OpenResult } from '$lib/terminal/peer';
	import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
	import PeerLinkCard from './PeerLinkCard.svelte';
	import { SelectField, Segmented } from '$lib/components/layout/view';

	let {
		probe,
		chatId = null,
		busy = false,
		disabledReason = null,
		inline = false,
		blurb = 'A shell for this chat. It keeps running when you close the panel.',
		onOpen,
		onCancel,
		onTargetsChanged,
		linkCardShownFor = null,
	}: {
		probe: Extract<ProbeState, { status: 'ready' }>;
		/** The chat this terminal belongs to, if any: signed so its own worker opens in its folder. */
		chatId?: string | null;
		busy?: boolean;
		/** The cap is reached: say why instead of opening. */
		disabledReason?: string | null;
		inline?: boolean;
		/** The empty state's line under the title. */
		blurb?: string;
		onOpen: (choice: CreateTerminal & { cwd: string | null; shell: string | null }) => void | Promise<OpenResult | void>;
		onCancel?: () => void;
		/** Ask for fresh targets, so the rows show what the worker now reports. */
		onTargetsChanged?: () => void;
		/** The worker whose link card the panel already shows, so the code appears once. */
		linkCardShownFor?: string | null;
	} = $props();

	const SOURCE: Record<string, string> = {
		worktree: "This chat's worktree",
		project: "This chat's project",
		workspace: 'Workspace folder',
		home: 'Home folder',
	};

	const targetKey = (target: TerminalTarget) => `${target.kind}:${target.worker_id ?? target.grant_id}`;
	// Reopening the popover returns to a peer whose request is still in flight;
	// otherwise a chat on another worker starts on that worker.
	// svelte-ignore state_referenced_locally
	const startingPeer = (() => {
		const local = localWorkerId(probe.targets);
		const candidates = (probe.targets ?? []).filter((target, index) => index > 0 && isPeerTarget(target, local));
		return candidates.find((target) => !!peerTerminals.flow(target.worker_id) && peerTerminals.flow(target.worker_id)?.phase !== 'ended')
			?? candidates.find((target) => target.worker_id === probe.chatWorkerId);
	})();
	let selected = $state(startingPeer ? targetKey(startingPeer) : '');
	// Seeded once from the probe; the owner edits from there.
	// svelte-ignore state_referenced_locally
	let folder = $state(startingPeer ? '' : probe.folder ?? '');
	// svelte-ignore state_referenced_locally
	let shell = $state(probe.defaultShell ?? probe.shells[0] ?? '');
	const destinations = $derived(probe.targets ?? []);
	const destination = $derived(selected ? destinations.find((target) => targetKey(target) === selected) : destinations[0]);
	const remote = $derived(!!selected);
	const unavailable = $derived(destination?.available === false || (remote && !destination));
	const ssh = $derived(destination?.kind === 'ssh');
	let input = $state<HTMLInputElement>();
	/** The peer refused the folder (`bad_folder`): fix it and open again. */
	let remoteFolder = $state<{ folder: string; message: string } | null>(null);
	const folderError = $derived(folder.trim() ? cwdProblem(folder.trim()) ?? '' : '');
	const remoteFolderError = $derived(remoteFolder && remoteFolder.folder === folder ? remoteFolder.message : '');

	// ── Peer workers (#196): only when the worker lists one ────────────────
	const localId = $derived(localWorkerId(destinations));
	const peers = $derived(destinations.some((target) => isPeerTarget(target, localId)));
	const peer = $derived(isPeerTarget(destination, localId) ? destination : undefined);
	const flow = $derived(peer ? peerTerminals.flow(peer.worker_id) : null);
	const keyProblem = $derived(probe.deviceKeyProblem ?? null);
	const stage = $derived.by(() => {
		if (!peer) return null;
		if (flow?.phase === 'linking' || flow?.phase === 'ended') return flow.phase;
		return selectable(peer, keyProblem) ? 'open' : 'blocked';
	});
	let opening = $state(false);
	const peerKey = (target: TerminalTarget) => `peer-reason-${(target.worker_id ?? target.grant_id ?? '').replace(/[^a-z0-9_-]/gi, '_')}`;
	const kindLabel = (target: TerminalTarget, index: number) =>
		index === 0 ? 'This worker' : target.kind === 'ssh' ? 'SSH grant' : 'Peer worker';

	$effect(() => { if (!inline) input?.focus(); });

	function pick(value: string) {
		selected = value;
		folder = selected ? '' : probe.folder ?? '';
	}

	async function openPeer() {
		if (!peer?.worker_id || opening) return;
		const workerId = peer.worker_id;
		const name = peer.name;
		const tried = folder;
		const cwd = folder.trim() || null;
		opening = true;
		try {
			// Only the chat's own worker knows the chat; any other worker opens in its default folder.
			const sessionId = probe.chatWorkerId === workerId ? chatId : null;
			const proof = await peerTerminals.authorize(workerId, name, ACTION_OPEN, { session_id: sessionId, cwd, shell: null });
			if (!proof) return;
			const result = await onOpen({ cwd, shell: null, target: { kind: 'worker', worker_id: workerId, proof } });
			remoteFolder = null;
			if (result && !result.ok && result.code === 'bad_folder') {
				remoteFolder = { folder: tried, message: `${name}: ${result.message || "that folder doesn't exist."}` };
				await tick();
				input?.focus();
			} else if (result && !result.ok) {
				peerTerminals.fail(workerId, name, `The shell on ${name} didn't open`, openErrorMessage(result.code, result.message, name));
				onTargetsChanged?.();
			}
		} finally {
			opening = false;
		}
	}

	function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy || disabledReason || folderError) return;
		if (peer) {
			if (stage === 'open' || (flow?.phase === 'ended' && flow.retry && selectable(peer, keyProblem))) void openPeer();
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
					{@const row = isPeerTarget(target, localId) ? peerRow(target, peerTerminals.flow(target.worker_id)?.phase, keyProblem) : null}
					{@const chip = row ? row.chip : target.available ? null : { label: 'Unavailable', tone: 'off' }}
					{@const reason = row?.reason ?? null}
					{@const pickable = index === 0 ? target.available : selectable(target, isPeerTarget(target, localId) ? keyProblem : null)}
					<label class="target" class:on={selected === value} class:off={!pickable}>
						<input type="radio" name="terminal-target" {value} checked={selected === value} disabled={!pickable}
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
		<div class="field"><span class="label">Target</span>
   <SelectField bind:value={selected} label="Target" options={destinations.map((target,index) => ({value:index === 0 ? '' : targetKey(target),label:`${target.name}${target.kind === 'ssh' ? ' (SSH grant)' : ''}`,disabled:!target.available}))} onchange={(value) => { pick(value); }} />
  </div>
	{/if}
	{#if peer}
		{#if flow?.phase === 'linking'}
			{#if flow.workerId !== linkCardShownFor}<PeerLinkCard {flow} />{/if}
		{:else if flow?.phase === 'ended'}
			<div class="ended" role="alert">
				<p class="p-title"><TriangleAlert size={14} aria-hidden="true" />{flow.title}</p>
				<p class="note">{flow.message}</p>
			</div>
		{/if}
		{#if stage === 'open' || flow?.phase === 'ended'}
			<label class="field">
				<span class="label">Folder</span>
				<span class="input-wrap" class:invalid={!!folderError || !!remoteFolderError}>
					<Folder size={14} aria-hidden="true" />
					<input bind:this={input} bind:value={folder} spellcheck="false" autocomplete="off" autocapitalize="off"
						placeholder={probe.chatWorkerId === peer.worker_id ? "This chat's folder" : `Default folder on ${peer.name}`} aria-describedby="terminal-folder-note" aria-invalid={!!folderError || !!remoteFolderError} />
				</span>
				<span class="note" id="terminal-folder-note" role={remoteFolderError ? 'alert' : undefined}>{#if folderError}{folderError}{:else if remoteFolderError}{remoteFolderError} Fix the folder, then open again.{:else}On {peer.name}. Leave blank for the default.{/if}</span>
			</label>
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
			<Segmented options={probe.shells} bind:value={shell} label="Shell" />
		</fieldset>
	{/if}
	{#if disabledReason}<p class="cap" role="alert">{disabledReason}</p>{/if}
	<div class="actions">
		{#if onCancel}<button type="button" class="st-btn" onclick={onCancel}>{stage === 'linking' ? 'Hide' : 'Cancel'}</button>{/if}
		{#if peer}
			{#if stage !== 'linking'}
				<button type="submit" class="st-btn primary" disabled={busy || opening || !!disabledReason || !!folderError || !!remoteFolderError || stage === 'blocked' || (flow?.phase === 'ended' && (!flow.retry || !selectable(peer, keyProblem)))}>{busy || opening ? 'Opening…' : flow?.phase === 'ended' ? 'Try again' : 'Open terminal'}</button>
			{/if}
		{:else}
		<button type="submit" class="st-btn primary" disabled={busy || !!disabledReason || !!folderError || unavailable}>
			{busy ? 'Opening…' : 'Open terminal'}
		</button>
		{/if}
	</div>
</form>

<style>
	.picker { display: grid; gap: 12px; padding: 14px; width: min(320px, calc(100vw - 32px)); box-sizing: border-box; font-family: var(--font-sans); }
	.picker:not(.inline) {
		border: 1px solid var(--menu-border); border-radius: var(--menu-radius); background: var(--popover);
		box-shadow: var(--menu-shadow); animation: drop 0.18s cubic-bezier(0.2, 0.8, 0.2, 1);
	}
	.picker.inline { width: min(360px, 100%); padding: 0; }
	.lede { display: flex; gap: 12px; align-items: flex-start; margin-bottom: 4px; }
	.glyph { display:grid; place-items:center; width:28px; height:28px; flex:none; color:var(--muted-foreground); }
	h3 { margin: 0; font-size: 14px; font-weight: 600; color: var(--foreground); }
	.lede p { margin: 3px 0 0; font-size: 12.5px; line-height: 1.45; color: var(--muted-foreground); }
	.field { display: grid; gap: 5px; margin: 0; padding: 0; border: 0; min-width: 0; }
	.label { padding: 0; font: 500 12px/1.4 var(--font-sans); color: var(--subtle-foreground); }
	.input-wrap { display: flex; align-items: center; gap: 8px; padding: 0 10px; min-height: 34px; border: 1px solid var(--input);
		border-radius: calc(var(--radius) - 4px); background: var(--code-bg); color: var(--muted-foreground); transition: border-color 0.15s ease-out, box-shadow 0.15s ease-out; }
	.input-wrap:focus-within { border-color: var(--ring); box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 20%, transparent); }
	.input-wrap.invalid { border-color: var(--destructive); }
	input:not([type='radio']) { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--foreground); font: 13px var(--st-mono); }
	.note { font-size: 11.5px; color: var(--muted-foreground); }
	.invalid + .note, .input-wrap.invalid ~ .note { color: var(--destructive); }
	.cap { margin: 0; padding: 8px 10px; border-radius: calc(var(--radius) - 4px); font-size: 12px; color: var(--warning);
		background: color-mix(in oklch, var(--warning) 11%, transparent); }
	.actions { display: flex; justify-content: flex-end; gap: 8px; }
	.inline .actions { justify-content: flex-start; }
	/* ── Peer targets (#196) ── */
	.picker.wide { width: min(372px, calc(100vw - 32px)); }
	.picker.inline.wide { width: min(400px, 100%); }
	.targets { display: grid; gap: 4px; }
	.target { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 2px 10px;
		padding: 8px 10px 8px 12px; border: 1px solid var(--border); border-radius: calc(var(--radius) - 4px); background: var(--code-bg);
		cursor: pointer; transition: border-color 0.15s ease-out, background-color 0.15s ease-out; }
	.target:hover:not(.off) { border-color: var(--border-strong); background: color-mix(in oklch, var(--hover-overlay) 60%, var(--code-bg)); }
	.target.on { border-color:var(--border); background:var(--row-selected); }
	.target.off { cursor: not-allowed; }
	.target.off .t-name { color: var(--muted-foreground); }
	.target input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: inherit; }
	.target:has(input:focus-visible) { outline: 2px solid var(--ring); outline-offset: 1px; }
	/* On a narrow row the kind drops under the name instead of cutting it short. */
	.t-text { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 8px; min-width: 0; }
	.t-name { max-width: 100%; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 600 12.5px var(--font-sans); color: var(--foreground); }
	.t-kind { flex-shrink: 0; font-size: 11px; color: var(--subtle-foreground); }
	.t-reason { grid-column: 1 / -1; font-size: 11.5px; line-height: 1.4; color: var(--muted-foreground); }
	.chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 7px; border-radius: 999px; white-space: nowrap;
		font: 600 10.5px/1.4 var(--font-sans); color: var(--muted-foreground); background: var(--muted); }
	.chip::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
	.chip[data-tone='ok'] { color: var(--success); background: color-mix(in oklch, var(--success) 13%, transparent); }
	.chip[data-tone='wait'] { color:var(--muted-foreground); background:var(--muted); }
	.chip[data-tone='warn'] { color: var(--warning); background: color-mix(in oklch, var(--warning) 13%, transparent); }
	.chip[data-tone='off']::before { background: var(--subtle-foreground); }
	.ended { display: grid; gap: 10px; padding: 12px; border-radius: calc(var(--radius) - 4px);
		border:1px solid var(--border); background:var(--muted);
		animation: drop 0.2s cubic-bezier(0.2, 0.8, 0.2, 1); }
	.ended { border-style: solid; border-color: color-mix(in oklab, var(--warning) 40%, var(--border)); background: color-mix(in oklch, var(--warning) 8%, transparent); }
	.ended p { margin: 0; line-height: 1.45; }
	.p-title { display: flex; align-items: center; gap: 7px; font: 600 12.5px var(--font-sans); color: var(--foreground); }
	.ended .p-title { color: var(--warning); }
	@keyframes drop { from { opacity: 0; transform: translateY(-4px) scale(0.98); } to { opacity: 1; transform: none; } }
	@media (max-width: 700px) { button, .input-wrap { min-height:44px; } input:not([type='radio']) { font-size:16px; } .target { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .picker:not(.inline), .ended { animation: none; } }
</style>
