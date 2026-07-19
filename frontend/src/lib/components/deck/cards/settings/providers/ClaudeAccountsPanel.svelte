<script lang="ts">
	/**
	 * ClaudeAccountsPanel — the saved-accounts sub-list for the Claude subscription
	 * row (switch / rename / delete / add), revealed by expanding that row.
	 *
	 * Lifted VERBATIM (mechanics unchanged, D8/A3) from ConnectionsSection's
	 * saved-accounts block: same server round-trips, same messages, same
	 * encryption-locked banner, same `sync_pending` badges. The ONLY change is the
	 * source of state/actions — this reads the `providers` store directly instead
	 * of prop-drilled callbacks (landmine L2). All `auth.checkAuth()` /
	 * `rateLimit.markStale()` side effects live in the store's actions.
	 */
	import { UserPlus, Check, X, Lock, Pencil, Trash2, Loader2, CircleAlert } from 'lucide-svelte';
	import type { ClaudeAccount } from '$lib/api/auth';
	import { providers } from '$lib/stores/providers.svelte';

	const s = $derived(providers.current);

	// ----- Account list local UI state (transient view state, as before) -----
	// Inline rename: which row is editing + its draft text. Delete confirm: which
	// row is armed. Both are transient, so they stay component-local.
	let renamingId: string | null = $state(null);
	let renameDraft = $state('');
	let confirmDeleteId: string | null = $state(null);
	// Whether to reveal the add-account panel (button → inline form). The panel
	// also stays open whenever an OAuth flow is mid-paste.
	let showAddAccount = $state(false);

	function beginRename(acc: ClaudeAccount) {
		renamingId = acc.id;
		renameDraft = acc.name || acc.email || '';
		confirmDeleteId = null;
	}
	function cancelRename() {
		renamingId = null;
		renameDraft = '';
	}
	function saveRename(id: string) {
		const next = renameDraft.trim();
		if (next) providers.renameAccount(id, next);
		renamingId = null;
		renameDraft = '';
	}
	function requestDelete(id: string) {
		confirmDeleteId = id;
		renamingId = null;
	}
	function confirmDelete(id: string) {
		confirmDeleteId = null;
		providers.deleteAccount(id);
	}

	// Client-side expiry check — the badge reads live from expires_at so it stays
	// accurate without a refetch. Null expiry = treat as not-expired.
	function isExpired(acc: ClaudeAccount): boolean {
		return acc.expires_at !== null && acc.expires_at < Date.now();
	}

	// Compact relative "last used" (e.g. "3h ago"). Returns '' when never used.
	function relativeLastUsed(ts: number | null): string {
		if (!ts) return '';
		const diff = Date.now() - ts;
		// Guard against non-finite input (NaN/Infinity) so a bad timestamp can
		// never render as "NaNy ago".
		if (!Number.isFinite(diff)) return '';
		if (diff < 0) return 'just now';
		const min = Math.floor(diff / 60000);
		if (min < 1) return 'just now';
		if (min < 60) return `${min}m ago`;
		const hr = Math.floor(min / 60);
		if (hr < 24) return `${hr}h ago`;
		const day = Math.floor(hr / 24);
		if (day < 30) return `${day}d ago`;
		const mo = Math.floor(day / 30);
		if (mo < 12) return `${mo}mo ago`;
		return `${Math.floor(mo / 12)}y ago`;
	}

	// The add-account panel is open when the user toggled it OR a flow is in progress.
	let addPanelOpen = $derived(showAddAccount || !!s.addAccountOAuthUrl);

	function openAddPanel() {
		showAddAccount = true;
	}
	function closeAddPanel() {
		showAddAccount = false;
		providers.cancelAddAccount();
	}
</script>

<div class="acct-block">
	<div class="acct-head">
		<h4>Saved accounts</h4>
		{#if s.accountsEncryptionReady && !addPanelOpen}
			<button type="button" class="ed-btn acct-add-btn" onclick={openAddPanel} disabled={s.accountsLoading}>
				<UserPlus />Add account
			</button>
		{/if}
	</div>

	{#if !s.accountsEncryptionReady}
		<!-- Locked store: the account list & switching are unavailable until the
		     admin re-derives the key by logging in again. -->
		<div class="kc-foot warn acct-locked" role="status">
			<Lock />
			<span>Accounts are encrypted with your admin password — log in again to unlock switching.</span>
		</div>
	{:else}
		{#if s.accountsError}
			<div class="kc-foot err" role="alert">{s.accountsError}</div>
		{/if}

		{#if s.accountsLoading && s.claudeAccounts.length === 0}
			<div class="acct-empty">Loading accounts…</div>
		{:else if s.claudeAccounts.length === 0 && !addPanelOpen}
			<div class="acct-empty">No saved accounts yet. Add one to switch between Claude sign-ins.</div>
		{/if}

		<ul class="acct-list">
			{#each s.claudeAccounts as acc (acc.id)}
				{@const expired = isExpired(acc)}
				{@const lastUsed = relativeLastUsed(acc.last_used_at)}
				<li class="acct-row" class:active={acc.is_active}>
					<div class="acct-ic" aria-hidden="true">
						{(acc.name || acc.email || '?').trim().charAt(0).toUpperCase()}
					</div>
					<div class="acct-body">
						{#if renamingId === acc.id}
							<!-- Inline rename: name field replaces the title until save/cancel. -->
							<div class="acct-rename">
								<input
									type="text"
									class="inp acct-rename-in"
									value={renameDraft}
									oninput={(e) => (renameDraft = e.currentTarget.value)}
									onkeydown={(e) => {
										if (e.key === 'Enter') saveRename(acc.id);
										if (e.key === 'Escape') cancelRename();
									}}
									placeholder="Account name"
									aria-label="Account name"
								/>
								<button type="button" class="acct-iconbtn ok" title="Save name" aria-label="Save name" onclick={() => saveRename(acc.id)}><Check /></button>
								<button type="button" class="acct-iconbtn" title="Cancel" aria-label="Cancel rename" onclick={cancelRename}><X /></button>
							</div>
						{:else}
							<div class="acct-name">
								<span class="acct-title">{acc.name || acc.email || 'Claude account'}</span>
								{#if acc.is_active}<span class="sb ok">Active</span>{/if}
								{#if expired}<span class="sb warn" title="This account's token has expired">Token expired</span>{/if}
								{#if !acc.has_refresh_token}<span class="sb warn" title="No refresh token — this sign-in can't renew automatically and will need re-authentication when it expires">No refresh token</span>{/if}
								{#if acc.sync_pending}<span class="sb warn acct-sync" title="Latest tokens not yet saved — unlock encryption by logging in again as admin"><CircleAlert />Sync pending</span>{/if}
							</div>
							<p class="acct-meta">
								<span>{acc.email || '—'}</span>
								{#if acc.subscription_type}<span class="acct-dot">·</span><span>{acc.subscription_type}</span>{/if}
								{#if lastUsed}<span class="acct-dot">·</span><span>used {lastUsed}</span>{/if}
							</p>
						{/if}
					</div>

					{#if renamingId !== acc.id}
						<div class="acct-acts">
							{#if confirmDeleteId === acc.id}
								<span class="acct-confirm">Delete?</span>
								<button type="button" class="ed-btn danger acct-sm" onclick={() => confirmDelete(acc.id)}>Delete</button>
								<button type="button" class="ed-btn acct-sm" onclick={() => (confirmDeleteId = null)}>Cancel</button>
							{:else}
								{#if !acc.is_active}
									<button
										type="button"
										class="ed-btn primary acct-sm"
										onclick={() => providers.activateAccount(acc.id)}
										disabled={s.switchingAccountId !== null}
									>
										{#if s.switchingAccountId === acc.id}<Loader2 class="spin" />Switching…{:else}Switch{/if}
									</button>
								{/if}
								<button type="button" class="acct-iconbtn" title="Rename" aria-label="Rename account" onclick={() => beginRename(acc)}><Pencil /></button>
								<button type="button" class="acct-iconbtn danger" title="Remove" aria-label="Remove account" onclick={() => requestDelete(acc.id)}><Trash2 /></button>
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>

		<!-- Add-account panel: same code-paste UX as the primary connect flow. -->
		{#if addPanelOpen}
			<div class="acct-addpanel">
				{#if !s.addAccountOAuthUrl}
					<p class="acct-addhint">A browser window opens to sign in. Paste the code it gives you back here — your current account stays active meanwhile.</p>
					<div class="acct-addrow">
						<button type="button" class="ed-btn primary" onclick={() => providers.startAddAccount()} disabled={s.addAccountLoading}>
							{s.addAccountLoading ? '…' : 'Open sign-in'}
						</button>
						<button type="button" class="ed-btn" onclick={closeAddPanel}>Cancel</button>
					</div>
				{:else}
					<div class="acct-addfields">
						<input
							type="text"
							class="inp"
							value={s.addAccountCode}
							oninput={(e) => providers.setAddAccountCode(e.currentTarget.value)}
							placeholder="Paste code from browser"
							aria-label="Authorization code"
						/>
						<input
							type="text"
							class="inp"
							value={s.addAccountName}
							oninput={(e) => providers.setAddAccountName(e.currentTarget.value)}
							placeholder="Account name (optional)"
							aria-label="Account name"
						/>
					</div>
					<label class="acct-check">
						<input
							type="checkbox"
							checked={s.addAccountMakeActive}
							onchange={(e) => providers.setAddAccountMakeActive(e.currentTarget.checked)}
						/>
						<span>Make active — switch the app to this account now</span>
					</label>
					<div class="acct-addrow">
						<button type="button" class="ed-btn primary" onclick={() => providers.completeAddAccount()} disabled={s.addAccountCompleting || !s.addAccountCode.trim()}>
							{s.addAccountCompleting ? '…' : 'Complete'}
						</button>
						<button type="button" class="ed-btn" onclick={closeAddPanel}>Cancel</button>
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	/* ===== Saved Claude accounts (relocated from ConnectionsSection) ===== */
	.acct-block {
		padding: 4px 4px 10px;
	}
	.acct-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin: 6px 0 8px;
	}
	.acct-head h4 {
		margin: 0;
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.acct-add-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.acct-add-btn :global(svg) {
		width: 14px;
		height: 14px;
	}
	.acct-locked {
		padding: 10px 0 2px;
	}
	.acct-empty {
		font-size: 12px;
		color: var(--muted-foreground);
		padding: 6px 0 4px;
	}
	.acct-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.acct-row {
		display: flex;
		gap: 12px;
		align-items: center;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.acct-row.active {
		border-color: color-mix(in oklch, var(--success, #16a34a) 45%, var(--border));
		background: color-mix(in oklch, var(--success, #16a34a) 7%, var(--muted));
	}
	.acct-ic {
		flex-shrink: 0;
		width: 32px;
		height: 32px;
		border-radius: 8px;
		display: grid;
		place-items: center;
		background: var(--background);
		color: var(--foreground);
		font-weight: 600;
		font-size: 14px;
	}
	.acct-body {
		flex: 1;
		min-width: 0;
	}
	.acct-name {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.acct-title {
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}
	.acct-meta {
		margin: 2px 0 0;
		font-size: 11.5px;
		color: var(--muted-foreground);
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	.acct-dot {
		opacity: 0.5;
	}
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.sb.warn {
		color: var(--warning, #d97706);
		background: color-mix(in oklch, var(--warning, #d97706) 16%, transparent);
	}
	.sb.acct-sync {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	.sb.acct-sync :global(svg) {
		width: 11px;
		height: 11px;
	}
	.acct-acts {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.acct-sm {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 5px 10px;
		font-size: 12px;
	}
	.acct-sm :global(svg) {
		width: 13px;
		height: 13px;
	}
	.acct-confirm {
		font-size: 12px;
		color: var(--warning, #d97706);
		font-weight: 600;
	}
	.acct-iconbtn {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.acct-iconbtn :global(svg) {
		width: 15px;
		height: 15px;
	}
	.acct-iconbtn:hover {
		color: var(--foreground);
		border-color: var(--foreground);
	}
	.acct-iconbtn.ok:hover {
		color: var(--success, #16a34a);
		border-color: var(--success, #16a34a);
	}
	.acct-iconbtn.danger:hover {
		color: var(--destructive);
		border-color: var(--destructive);
	}
	.acct-iconbtn:focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 1px;
	}
	.acct-rename {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.acct-rename-in {
		flex: 1;
		min-width: 0;
		max-width: 280px;
	}
	.acct-addpanel {
		margin-top: 10px;
		padding: 12px;
		border: 1px dashed var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.acct-addhint {
		margin: 0 0 10px;
		font-size: 12px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.acct-addfields {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.acct-addfields .inp {
		width: 100%;
	}
	.acct-check {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 10px 0;
		font-size: 12px;
		color: var(--foreground);
		cursor: pointer;
	}
	.acct-check input {
		width: 15px;
		height: 15px;
		accent-color: var(--success, #16a34a);
	}
	.acct-addrow {
		display: flex;
		gap: 8px;
	}
	.kc-foot {
		padding: 8px 0 4px;
		font-size: 12.5px;
	}
	.kc-foot.err {
		color: var(--destructive);
	}
	.kc-foot.warn {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		color: var(--warning, #d97706);
		line-height: 1.5;
	}
	.kc-foot.warn :global(svg) {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		margin-top: 1px;
	}
	/* Switch-in-progress spinner (lucide Loader2). */
	:global(.acct-sm .spin) {
		animation: acct-spin 0.8s linear infinite;
	}
	@keyframes acct-spin {
		to { transform: rotate(360deg); }
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.acct-sm .spin) {
			animation: none;
		}
	}

	@media (max-width: 560px) {
		.acct-row {
			flex-wrap: wrap;
		}
		.acct-acts {
			width: 100%;
			justify-content: flex-start;
			padding-left: 44px;
		}
	}
</style>
