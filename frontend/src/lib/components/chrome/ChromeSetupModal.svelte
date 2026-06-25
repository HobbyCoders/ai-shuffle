<script lang="ts">
	/**
	 * One-time "Set up browser" panel for Claude in Chrome.
	 *
	 * Shows a live, interactive view of the headless container browser so the
	 * user can sign the extension into their Claude account and approve pairing —
	 * without exposing a DevTools port. Polls targets so a sign-in tab opened by
	 * the extension is followed automatically, and polls status so the "Paired"
	 * state updates live.
	 */
	import UniversalModal from '$lib/components/UniversalModal.svelte';
	import ChromeScreencast from './ChromeScreencast.svelte';
	import {
		getChromeTargets,
		getChromeMcpStatus,
		pairChromeMcp,
		type CdpTarget,
		type ChromeMcpStatus
	} from '$lib/api/chromeMcp';

	interface Props {
		open: boolean;
		onClose: () => void;
		/** Called with the latest status whenever it changes (so the parent refreshes). */
		onStatus?: (status: ChromeMcpStatus) => void;
	}

	let { open, onClose, onStatus }: Props = $props();

	let targets = $state<CdpTarget[]>([]);
	let selectedTargetId = $state<string | undefined>(undefined);
	let status = $state<ChromeMcpStatus | null>(null);
	let connState = $state('connecting');
	let urlInput = $state('');
	let screencast = $state<ChromeScreencast | null>(null);
	let knownPageIds = new Set<string>();
	let firstPoll = true;
	let pairing = $state(false);
	let pairMsg = $state('');
	// Auto-pair once per panel-open when we first see a connected-but-unpinned
	// browser, so the common single-browser case pins itself with no extra click.
	let autoPairTried = false;

	function pageTargets(list: CdpTarget[]): CdpTarget[] {
		return list.filter((t) => t.type === 'page');
	}

	async function refreshTargets() {
		try {
			const list = await getChromeTargets();
			targets = list;
			const pages = pageTargets(list);
			const ids = new Set(pages.map((p) => p.id));
			if (firstPoll) {
				firstPoll = false;
				knownPageIds = ids;
				if (!selectedTargetId && pages.length) selectedTargetId = pages[0].id;
			} else {
				// Follow a newly-opened tab (e.g. the extension's sign-in window).
				const fresh = pages.find((p) => !knownPageIds.has(p.id));
				if (fresh) selectedTargetId = fresh.id;
				knownPageIds = ids;
				// Selected tab closed — fall back to any remaining page.
				if (selectedTargetId && !ids.has(selectedTargetId)) {
					selectedTargetId = pages.length ? pages[0].id : undefined;
				}
			}
		} catch {
			/* transient — keep last known targets */
		}
	}

	async function refreshStatus() {
		try {
			const s = await getChromeMcpStatus();
			status = s;
			onStatus?.(s);
			// Idempotent auto-pair: once per open, when connected but not yet
			// pinned, pin this container's browser silently. The backend no-ops if
			// already paired and returns a clear message (e.g. AMBIGUOUS) if it
			// can't decide, which we surface without spamming.
			if (s.connected && !s.paired && !autoPairTried && !pairing) {
				autoPairTried = true;
				void pair(true);
			}
		} catch {
			/* ignore */
		}
	}

	async function pair(auto = false) {
		if (pairing) return;
		pairing = true;
		if (!auto) pairMsg = '';
		try {
			const res = await pairChromeMcp();
			status = res.status;
			onStatus?.(res.status);
			pairMsg = res.message;
		} catch (e: any) {
			pairMsg = e?.detail || 'Failed to pair browser';
		} finally {
			pairing = false;
		}
	}

	function go() {
		const u = urlInput.trim();
		if (u && screencast) screencast.navigate(/^[a-z]+:\/\//i.test(u) ? u : `https://${u}`);
	}

	function openExtensionSettings() {
		if (status?.extension_options_url && screencast) {
			screencast.navigate(status.extension_options_url);
		}
	}

	function reload() {
		if (screencast) screencast.navigate('javascript:location.reload()');
	}

	// Poll targets + status while the panel is open.
	$effect(() => {
		if (!open) return;
		firstPoll = true;
		knownPageIds = new Set();
		autoPairTried = false;
		pairMsg = '';
		refreshTargets();
		refreshStatus();
		const t = setInterval(refreshTargets, 2500);
		const s = setInterval(refreshStatus, 3000);
		return () => {
			clearInterval(t);
			clearInterval(s);
		};
	});
</script>

<UniversalModal {open} title="Set up Claude in Chrome" size="full" showFooter={false} {onClose}>
	{#snippet children()}
		<div class="setup">
			<p class="intro">
				Sign the <strong>Claude</strong> extension into your Claude account, then approve pairing.
				The browser below is the real headless Chrome running in your container — interact with it
				directly. A sign-in window opened by the extension appears here automatically.
			</p>

			<div class="toolbar">
				<span class="dot" class:live={connState === 'live'} class:err={connState === 'error'}></span>
				<span
					class="conn"
					title="Live view of the headless browser. This only means the screen is streaming — it is not the extension/account connection (see the status badge)."
					>{connState === 'live' ? 'Live view' : connState}</span
				>

				{#if status}
					{#if status.paired}
						<span class="sb ok">Pinned{status.paired_device_name ? `: ${status.paired_device_name}` : ''}</span>
					{:else if status.connected}
						<span class="sb ok">Connected</span>
					{:else}
						<span class="sb off">Not connected</span>
					{/if}
				{/if}

				<div class="spacer"></div>

				{#if status?.connected && !status.paired}
					<button type="button" class="btn primary" onclick={() => pair()} disabled={pairing}>
						{pairing ? 'Pairing…' : 'Pair this browser'}
					</button>
				{/if}
				{#if pageTargets(targets).length > 1}
					<select class="sel" bind:value={selectedTargetId} aria-label="Browser tab">
						{#each pageTargets(targets) as t (t.id)}
							<option value={t.id}>{t.title || t.url || t.id}</option>
						{/each}
					</select>
				{/if}
				{#if status?.extension_options_url}
					<button type="button" class="btn" onclick={openExtensionSettings}>Extension sign-in</button>
				{/if}
				<button type="button" class="btn" onclick={reload}>Reload</button>
			</div>

			<div class="urlbar">
				<input
					class="inp"
					type="text"
					placeholder="Go to URL (e.g. claude.ai)"
					bind:value={urlInput}
					onkeydown={(e) => e.key === 'Enter' && go()}
				/>
				<button type="button" class="btn primary" onclick={go}>Go</button>
			</div>

			<div class="stage">
				<ChromeScreencast
					bind:this={screencast}
					targetId={selectedTargetId}
					onState={(s) => (connState = s)}
				/>
			</div>

			{#if status?.paired}
				<p class="done">✓ Pinned to this browser. You can close this panel — the agent will
					use it on future sessions, even after a restart and even if another browser is
					signed into this Claude account.</p>
			{:else if status?.connected}
				<p class="done">✓ Signed in and connected. {pairMsg ||
						'Pinning this browser so it stays selected…'}</p>
			{:else if status}
				<!-- Not connected yet. The live view above may be streaming, but the
				     extension still needs to be installed/signed in. Show the backend's
				     concrete next step so the panel never looks "done" when it isn't. -->
				<p class="pairmsg">{pairMsg || status.next_step}</p>
			{:else if pairMsg}
				<p class="pairmsg">{pairMsg}</p>
			{/if}
		</div>
	{/snippet}
</UniversalModal>

<style>
	.setup {
		display: flex;
		flex-direction: column;
		gap: 12px;
		height: 100%;
		min-height: 0;
	}
	.intro {
		margin: 0;
		font-size: 13px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.toolbar,
	.urlbar {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.spacer {
		flex: 1;
	}
	.dot {
		width: 9px;
		height: 9px;
		border-radius: 50%;
		background: var(--warning, #d59f0f);
		flex: none;
	}
	.dot.live {
		background: var(--success, #16a34a);
	}
	.dot.err {
		background: var(--destructive, #dc2626);
	}
	.conn {
		font-size: 12px;
		color: var(--muted-foreground);
		text-transform: capitalize;
		min-width: 78px;
	}
	.urlbar .inp {
		flex: 1;
	}
	.stage {
		flex: 1;
		min-height: 360px;
		display: flex;
	}
	.done {
		margin: 0;
		font-size: 13px;
		color: var(--success, #16a34a);
		font-weight: 500;
	}
	.pairmsg {
		margin: 0;
		font-size: 13px;
		color: var(--muted-foreground);
	}
	.sb {
		font-size: 10px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 7px;
		border-radius: 999px;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in srgb, var(--success, #16a34a) 14%, transparent);
	}
	.sb.off {
		color: var(--muted-foreground);
		background: var(--muted);
	}
</style>
