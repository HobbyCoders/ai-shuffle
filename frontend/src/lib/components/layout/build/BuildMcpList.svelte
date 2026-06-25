<script lang="ts">
	/**
	 * BuildMcpList — compact rows presentation of MCP servers for the Build page.
	 *
	 * Header-less by design (BuildPage supplies the heading + sub-nav). Reuses
	 * McpManagerCard's data + editor flow; only the list view is new. Theme tokens
	 * only (matches the Studio-v2 mockup's `.listrows` layout, not its palette).
	 */
	import type { McpServerInfo } from '$lib/api/mcp';
	import { getServerTypeLabel, getScopeLabel } from '$lib/api/mcp';
	import { Server, Terminal, Zap, Globe, Trash2, Plus, Search, Info, AlertCircle, X } from 'lucide-svelte';

	interface Props {
		servers: McpServerInfo[];
		error?: string | null;
		onEdit: (server: McpServerInfo) => void;
		onNew: () => void;
		onDelete: (name: string) => Promise<void> | void;
		onClearError?: () => void;
	}

	let { servers, error = null, onEdit, onNew, onDelete, onClearError }: Props = $props();

	let searchQuery = $state('');
	let pendingName = $state<string | null>(null);
	let pendingLoading = $state(false);

	const filtered = $derived(
		searchQuery.trim()
			? servers.filter(
					(s) =>
						s.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
						(s.description && s.description.toLowerCase().includes(searchQuery.toLowerCase()))
				)
			: servers
	);

	function typeIcon(type: string) {
		switch (type) {
			case 'stdio': return Terminal;
			case 'sse': return Zap;
			case 'http': return Globe;
			default: return Server;
		}
	}

	function detailLine(s: McpServerInfo): string {
		if (s.type === 'stdio' && s.command) return s.command + (s.args?.length ? ' ' + s.args.join(' ') : '');
		if ((s.type === 'sse' || s.type === 'http') && s.url) return s.url;
		if (s.cli_flag) return `CLI flag · --${s.cli_flag}`;
		if (s.builtin) return 'In-process SDK server';
		return getServerTypeLabel(s.type);
	}

	async function confirmDelete() {
		if (!pendingName) return;
		pendingLoading = true;
		try {
			await onDelete(pendingName);
			pendingName = null;
		} catch (e) {
			console.error('Failed to remove MCP server:', e);
		} finally {
			pendingLoading = false;
		}
	}
</script>

<div class="bml">
	<div class="bml-toolbar">
		<div class="bml-search">
			<Search size={14} strokeWidth={1.7} />
			<input type="text" placeholder="Search servers…" autocomplete="off" bind:value={searchQuery} />
		</div>
	</div>

	<div class="bml-note">
		<Info size={14} strokeWidth={1.7} />
		<span>Add and remove servers here. To enable or disable them per project, use the MCP tab in the Projects card.</span>
	</div>

	{#if error}
		<div class="bml-error">
			<AlertCircle size={15} strokeWidth={1.7} />
			<span>{error}</span>
			{#if onClearError}
				<button class="bml-error-close" onclick={onClearError} aria-label="Dismiss"><X size={14} strokeWidth={2} /></button>
			{/if}
		</div>
	{/if}

	{#snippet rowInner(server: McpServerInfo)}
		{@const Icon = typeIcon(server.type)}
		<span class="bml-ic"><Icon size={17} strokeWidth={1.7} /></span>
		<div class="bml-body">
			<h4 class="bml-name">
				{server.name}
				<span class="bml-scope">{getScopeLabel(server.scope)}</span>
			</h4>
			<p class="bml-desc">{detailLine(server)}</p>
		</div>
		<div class="bml-meta">
			<span class="bml-tag">{getServerTypeLabel(server.type)}</span>
			<span class="bml-dot" class:on={server.enabled} title={server.enabled ? 'Enabled' : 'Disabled'}></span>
		</div>
		{#if server.builtin}
			<!-- Empty buffer keeps the meta column aligned with deletable (custom) rows. -->
			<div class="bml-actions"></div>
		{:else}
			<div class="bml-actions" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
				<button class="bml-act bml-act--danger" title="Delete" onclick={() => (pendingName = server.name)}>
					<Trash2 size={15} strokeWidth={1.7} />
				</button>
			</div>
		{/if}
	{/snippet}

	<div class="bml-list">
		{#each filtered as server (server.name)}
			{#if pendingName === server.name}
				<div class="bml-row bml-row--confirm">
					<p class="bml-confirm-text">Delete <strong>{server.name}</strong>? This can't be undone.</p>
					<div class="bml-confirm-actions">
						<button class="bml-btn" onclick={() => (pendingName = null)} disabled={pendingLoading}>Cancel</button>
						<button class="bml-btn bml-btn--danger" onclick={confirmDelete} disabled={pendingLoading}>Delete</button>
					</div>
				</div>
			{:else if server.builtin}
				<div class="bml-row bml-row--static">{@render rowInner(server)}</div>
			{:else}
				<div
					class="bml-row"
					role="button"
					tabindex="0"
					onclick={() => onEdit(server)}
					onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onEdit(server); } }}
				>{@render rowInner(server)}</div>
			{/if}
		{/each}

		<button class="bml-new" onclick={onNew}>
			<span class="bml-new-plus"><Plus size={16} strokeWidth={2.2} /></span>
			<span>Add server</span>
		</button>
	</div>

	{#if filtered.length === 0 && searchQuery.trim()}
		<p class="bml-empty">No servers match “{searchQuery}”.</p>
	{/if}
</div>

<style>
	.bml { display: flex; flex-direction: column; gap: 14px; }

	/* Toolbar */
	.bml-toolbar { display: flex; align-items: center; gap: 12px; }
	.bml-search { position: relative; display: flex; align-items: center; flex: 1; max-width: 320px; }
	.bml-search :global(svg) { position: absolute; left: 11px; color: var(--muted-foreground); pointer-events: none; }
	.bml-search input {
		width: 100%; height: 34px; padding: 0 12px 0 32px;
		background: var(--input-bg); border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground); font-size: 0.8125rem; outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.bml-search input::placeholder { color: var(--muted-foreground); }
	.bml-search input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }

	/* Info note */
	.bml-note {
		display: flex; align-items: flex-start; gap: 8px;
		padding: 10px 12px; border-radius: calc(var(--radius, 0.75rem) - 2px);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		color: var(--muted-foreground); font-size: 0.75rem; line-height: 1.4;
	}
	.bml-note :global(svg) { flex-shrink: 0; color: var(--primary); margin-top: 1px; }

	/* Error */
	.bml-error {
		display: flex; align-items: center; gap: 8px;
		padding: 10px 12px; border-radius: calc(var(--radius, 0.75rem) - 2px);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive); font-size: 0.8125rem;
	}
	.bml-error-close { margin-left: auto; background: transparent; border: none; color: var(--destructive); cursor: pointer; padding: 2px; border-radius: 4px; display: grid; place-items: center; }

	/* List */
	.bml-list {
		display: flex; flex-direction: column;
		border: 1px solid var(--border); border-radius: var(--radius, 0.75rem);
		overflow: hidden; background: var(--card);
	}
	.bml-row {
		display: flex; align-items: center; gap: 14px;
		padding: 13px 16px; min-height: 60px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		cursor: pointer; transition: background-color 120ms ease;
	}
	.bml-row:last-child { border-bottom: none; }
	.bml-row:hover { background: var(--hover-overlay); }
	.bml-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.bml-row--static { cursor: default; }
	.bml-row--static:hover { background: transparent; }

	.bml-ic {
		display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0;
		border-radius: calc(var(--radius, 0.75rem) - 4px);
		background: color-mix(in oklch, var(--primary) 12%, transparent); color: var(--primary);
	}
	.bml-body { flex: 1; min-width: 0; }
	.bml-name {
		display: flex; align-items: center; gap: 8px; margin: 0 0 3px;
		font-family: var(--font-display, Georgia, serif);
		font-size: 0.9rem; font-weight: 700; letter-spacing: -0.01em; color: var(--foreground);
		overflow: hidden;
	}
	.bml-scope {
		flex-shrink: 0; font-family: var(--font-mono, monospace);
		font-size: 0.58rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase;
		color: var(--primary-foreground); background: var(--primary);
		padding: 2px 6px; border-radius: 5px;
	}
	.bml-desc {
		margin: 0; font-size: 0.75rem; color: var(--muted-foreground); line-height: 1.4;
		font-family: var(--font-mono, monospace);
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}

	.bml-meta { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
	.bml-tag {
		font-size: 0.6875rem; font-weight: 500; color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		padding: 2px 8px; border-radius: 6px; white-space: nowrap;
	}
	.bml-dot { width: 8px; height: 8px; border-radius: 50%; background: color-mix(in oklch, var(--muted-foreground) 50%, transparent); flex-shrink: 0; }
	.bml-dot.on { background: var(--success, #3b9c6e); }

	/* Fixed width reserves the delete-button slot so built-in (non-deletable) rows
	   keep their meta column aligned with custom rows. */
	.bml-actions { display: flex; justify-content: flex-end; gap: 2px; flex-shrink: 0; width: 28px; opacity: 0; transition: opacity 120ms ease; }
	.bml-row:hover .bml-actions, .bml-row:focus-within .bml-actions { opacity: 1; }
	.bml-act {
		display: grid; place-items: center; width: 28px; height: 28px;
		background: transparent; border: 1px solid transparent; border-radius: 7px;
		color: var(--muted-foreground); cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.bml-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Inline confirm */
	.bml-row--confirm { justify-content: space-between; cursor: default; background: color-mix(in oklch, var(--destructive) 6%, var(--card)); }
	.bml-confirm-text { margin: 0; font-size: 0.8rem; color: var(--foreground); }
	.bml-confirm-actions { display: flex; gap: 8px; flex-shrink: 0; }
	.bml-btn {
		display: inline-flex; align-items: center; height: 30px; padding: 0 12px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 3px);
		font-size: 0.8rem; font-weight: 500; cursor: pointer;
	}
	.bml-btn:hover { background: var(--hover-overlay); }
	.bml-btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.bml-btn--danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 35%, var(--border)); }
	.bml-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* New row */
	.bml-new {
		display: flex; align-items: center; justify-content: center; gap: 8px;
		padding: 13px 16px; min-height: 52px;
		background: transparent; border: none; border-top: 1px dashed color-mix(in oklch, var(--muted-foreground) 35%, transparent);
		color: var(--muted-foreground); font-size: 0.8rem; font-weight: 600; cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.bml-new:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bml-new-plus { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: var(--primary); color: var(--primary-foreground); }

	.bml-empty { font-size: 0.8rem; color: var(--muted-foreground); text-align: center; padding: 8px 0; }

	@media (max-width: 639px) {
		.bml-search { max-width: none; }
		.bml-meta { display: none; }
		.bml-actions { opacity: 1; }
	}
</style>
