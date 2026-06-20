<script lang="ts">
	/**
	 * BuildHookList — header-less Hooks surface for the Build page.
	 *
	 * Matches the Subagents/MCP/Plugins row style + toolbar layout (search left,
	 * actions right) with left-aligned underline App / Project / Plugin tabs that
	 * mirror the Build page sub-nav. Reuses HooksCard's
	 * data + editor flow; only the list view is new. Theme tokens only.
	 */
	import type { Hook, PluginHookInfo, HookTestResult } from '$lib/api/hooks';
	import {
		Search, Plus, Terminal, Globe, MessageSquare, Zap, Play, Trash2,
		AlertCircle, CheckCircle, XCircle, X, Loader2, FileCode
	} from 'lucide-svelte';

	type Tab = 'app' | 'project' | 'plugins';

	interface Props {
		allHooks: Hook[];
		pluginHooks: PluginHookInfo[];
		hasProject: boolean;
		loading?: boolean;
		error?: string | null;
		testResult?: { hookId: string; result: HookTestResult } | null;
		testing?: Record<string, boolean>;
		saving?: Record<string, boolean>;
		onCreate: () => void;
		onEdit: (hook: Hook) => void;
		onDelete: (id: string) => Promise<void> | void;
		onToggle: (id: string) => void;
		onTest: (id: string) => void;
		onClearError: () => void;
		onClearTest: () => void;
	}

	let {
		allHooks,
		pluginHooks,
		hasProject,
		loading = false,
		error = null,
		testResult = null,
		testing = {},
		saving = {},
		onCreate,
		onEdit,
		onDelete,
		onToggle,
		onTest,
		onClearError,
		onClearTest
	}: Props = $props();

	let activeTab = $state<Tab>('app');
	let searchQuery = $state('');
	let pendingDelete = $state<string | null>(null);
	let pendingLoading = $state(false);

	const eventColors: Record<string, string> = {
		PreToolUse: 'var(--info)',
		PostToolUse: 'var(--info)',
		Stop: 'var(--success)',
		SessionStart: 'var(--primary)',
		SessionEnd: 'var(--primary)',
		UserPromptSubmit: 'var(--warning)',
		PreCompact: 'var(--muted-foreground)',
		SubagentStop: 'var(--info)',
		Notification: 'var(--warning)'
	};
	function eventColor(event: string): string {
		return eventColors[event] || 'var(--muted-foreground)';
	}

	const appHooks = $derived(allHooks.filter((h) => !h.project_id));
	const scopedHooks = $derived(allHooks.filter((h) => !!h.project_id));

	function matches(h: Hook, q: string): boolean {
		return (
			h.name.toLowerCase().includes(q) ||
			h.event.toLowerCase().includes(q) ||
			(h.command?.toLowerCase().includes(q) ?? false) ||
			(h.url?.toLowerCase().includes(q) ?? false)
		);
	}
	function filterHooks(list: Hook[]): Hook[] {
		const q = searchQuery.trim().toLowerCase();
		return q ? list.filter((h) => matches(h, q)) : list;
	}

	const filteredApp = $derived(filterHooks(appHooks));
	const filteredProject = $derived(filterHooks(scopedHooks));
	const filteredPlugins = $derived.by(() => {
		const q = searchQuery.trim().toLowerCase();
		if (!q) return pluginHooks;
		return pluginHooks.filter(
			(h) =>
				h.plugin_name.toLowerCase().includes(q) ||
				h.event.toLowerCase().includes(q) ||
				(h.description?.toLowerCase().includes(q) ?? false)
		);
	});

	const tabs = $derived([
		{ id: 'app' as Tab, label: 'App', count: appHooks.length },
		{ id: 'project' as Tab, label: 'Project', count: scopedHooks.length },
		{ id: 'plugins' as Tab, label: 'Plugin', count: pluginHooks.length }
	]);

	function selectTab(id: Tab) {
		if (id === activeTab) return;
		activeTab = id;
		pendingDelete = null;
	}

	// Icon by hook delivery type.
	function typeIcon(t: string) {
		if (t === 'webhook') return Globe;
		if (t === 'message') return MessageSquare;
		if (t === 'bash') return Terminal;
		return Zap;
	}
	function detailText(h: Hook): string {
		if (h.hook_type === 'bash') return h.command ?? '';
		if (h.hook_type === 'webhook') return h.url ?? '';
		if (h.hook_type === 'message') return h.message_template ?? '';
		return '';
	}

	async function confirmDelete() {
		if (!pendingDelete) return;
		pendingLoading = true;
		try {
			await onDelete(pendingDelete);
			pendingDelete = null;
		} catch (e) {
			console.error('Hook delete failed:', e);
		} finally {
			pendingLoading = false;
		}
	}
</script>

<div class="bhl">
	<!-- Tabs (underline sub-nav, left-aligned) -->
	<div class="bhl-tabnav">
		<div class="bhl-tabs">
			{#each tabs as tab}
				<button class="bhl-tab" class:on={activeTab === tab.id} onclick={() => selectTab(tab.id)}>
					{tab.label}<span class="bhl-tab-count">{tab.count}</span>
				</button>
			{/each}
		</div>
	</div>

	<!-- Toolbar: search left, actions right -->
	<div class="bhl-toolbar">
		<div class="bhl-search">
			<Search size={14} strokeWidth={1.7} />
			<input type="text" placeholder="Search hooks…" autocomplete="off" bind:value={searchQuery} />
		</div>
		<div class="bhl-tools">
			{#if activeTab !== 'plugins'}
				<button class="bhl-btn bhl-btn--primary" onclick={onCreate}>
					<Plus size={15} strokeWidth={2} /><span>New hook</span>
				</button>
			{/if}
		</div>
	</div>

	<!-- Test result toast -->
	{#if testResult}
		<div class="bhl-toast" class:ok={testResult.result.success} class:bad={!testResult.result.success}>
			{#if testResult.result.success}
				<CheckCircle size={15} strokeWidth={1.8} />
				<span>Hook test successful{testResult.result.duration_ms != null ? ` (${testResult.result.duration_ms}ms)` : ''}</span>
			{:else}
				<XCircle size={15} strokeWidth={1.8} />
				<span>Hook test failed: {testResult.result.error}</span>
			{/if}
			<button class="bhl-toast-close" onclick={onClearTest} aria-label="Dismiss"><X size={14} strokeWidth={2} /></button>
		</div>
	{/if}

	<!-- Error -->
	{#if error}
		<div class="bhl-error">
			<AlertCircle size={15} strokeWidth={1.7} />
			<span>{error}</span>
			<button class="bhl-error-close" onclick={onClearError} aria-label="Dismiss"><X size={14} strokeWidth={2} /></button>
		</div>
	{/if}

	{#if loading}
		<div class="bhl-loading"><Loader2 size={28} class="bhl-spin" /><p>Loading hooks…</p></div>

	<!-- ─── APP / PROJECT (editable) ─── -->
	{:else if activeTab === 'app' || activeTab === 'project'}
		{@const list = activeTab === 'app' ? filteredApp : filteredProject}
		{#if activeTab === 'project' && !hasProject}
			<div class="bhl-empty">
				<Zap size={34} strokeWidth={1.25} />
				<p class="bhl-empty-title">No project selected</p>
				<p class="bhl-empty-sub">Select a project to manage its hooks</p>
			</div>
		{:else if list.length === 0}
			<div class="bhl-empty">
				<Zap size={34} strokeWidth={1.25} />
				<p class="bhl-empty-title">
					{searchQuery.trim()
						? 'No hooks match your search'
						: activeTab === 'app' ? 'No app-level hooks' : 'No project hooks'}
				</p>
				{#if !searchQuery.trim()}
					{#if activeTab === 'app'}<p class="bhl-empty-sub">App hooks fire for all sessions, regardless of project</p>{/if}
					<button class="bhl-link" onclick={onCreate}>Add your first hook</button>
				{/if}
			</div>
		{:else}
			<div class="bhl-list">
				{#each list as hook (hook.id)}
					{#if pendingDelete === hook.id}
						<div class="bhl-row bhl-row--confirm">
							<p class="bhl-confirm-text">Delete <strong>{hook.name}</strong>? This can't be undone.</p>
							<div class="bhl-confirm-actions">
								<button class="bhl-btn bhl-btn--sm" onclick={() => (pendingDelete = null)} disabled={pendingLoading}>Cancel</button>
								<button class="bhl-btn bhl-btn--sm bhl-btn--danger" onclick={confirmDelete} disabled={pendingLoading}>Delete</button>
							</div>
						</div>
					{:else}
						{@const TypeIcon = typeIcon(hook.hook_type)}
						<div
							class="bhl-row"
							class:bhl-row--off={!hook.enabled}
							role="button"
							tabindex="0"
							onclick={() => onEdit(hook)}
							onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onEdit(hook); } }}
						>
							<span class="bhl-ic"><TypeIcon size={17} strokeWidth={1.7} /></span>
							<div class="bhl-body">
								<h4 class="bhl-name">
									{hook.name}
									<span class="bhl-badge" style="background: color-mix(in oklch, {eventColor(hook.event)} 15%, transparent); color: {eventColor(hook.event)};">{hook.event}</span>
									{#if hook.tool_matcher}<span class="bhl-badge bhl-badge--mono">{hook.tool_matcher}</span>{/if}
									{#if hook.failure_count > 0}<span class="bhl-badge bhl-badge--warn">{hook.failure_count} fail{hook.failure_count > 1 ? 's' : ''}</span>{/if}
								</h4>
								{#if detailText(hook)}<p class="bhl-desc" class:mono={hook.hook_type === 'bash'}>{detailText(hook)}</p>{/if}
							</div>
							<div class="bhl-meta">
								<span class="bhl-tag">{hook.hook_type}</span>
							</div>
							<div class="bhl-actions" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
								<button class="bhl-act" title="Test hook" onclick={() => onTest(hook.id)} disabled={testing[hook.id] || saving[hook.id]}>
									{#if testing[hook.id]}<Loader2 size={15} class="bhl-spin" />{:else}<Play size={15} strokeWidth={1.7} />{/if}
								</button>
								<button class="bhl-act bhl-act--danger" title="Delete hook" onclick={() => (pendingDelete = hook.id)} disabled={saving[hook.id]}>
									<Trash2 size={15} strokeWidth={1.7} />
								</button>
							</div>
							<button
								class="bhl-toggle"
								class:on={hook.enabled}
								title={hook.enabled ? 'Disable hook' : 'Enable hook'}
								onclick={(e) => { e.stopPropagation(); onToggle(hook.id); }}
								disabled={saving[hook.id]}
								aria-label={hook.enabled ? 'Disable hook' : 'Enable hook'}
							>
								<span class="bhl-toggle-thumb"></span>
							</button>
						</div>
					{/if}
				{/each}

				<button class="bhl-new" onclick={onCreate}>
					<span class="bhl-new-plus"><Plus size={16} strokeWidth={2.2} /></span>
					<span>New {activeTab === 'project' ? 'project' : 'app'} hook</span>
				</button>
			</div>
		{/if}

	<!-- ─── PLUGINS (read-only) ─── -->
	{:else}
		{#if filteredPlugins.length === 0}
			<div class="bhl-empty">
				<FileCode size={34} strokeWidth={1.25} />
				<p class="bhl-empty-title">{searchQuery.trim() ? 'No plugin hooks match your search' : 'No plugin hooks installed'}</p>
				{#if !searchQuery.trim()}<p class="bhl-empty-sub">Install plugins with hooks to see them here</p>{/if}
			</div>
		{:else}
			<div class="bhl-list">
				{#each filteredPlugins as hook (hook.plugin_id + hook.event)}
					{@const TypeIcon = typeIcon(hook.hook_type)}
					<div class="bhl-row bhl-row--static">
						<span class="bhl-ic bhl-ic--muted"><TypeIcon size={17} strokeWidth={1.7} /></span>
						<div class="bhl-body">
							<h4 class="bhl-name">
								{hook.plugin_name}
								<span class="bhl-badge" style="background: color-mix(in oklch, {eventColor(hook.event)} 15%, transparent); color: {eventColor(hook.event)};">{hook.event}</span>
								{#if hook.tool_matcher}<span class="bhl-badge bhl-badge--mono">{hook.tool_matcher}</span>{/if}
								<span class="bhl-badge">Plugin</span>
							</h4>
							{#if hook.description}<p class="bhl-desc">{hook.description}</p>{/if}
						</div>
						<div class="bhl-meta">
							<span class="bhl-tag">{hook.hook_type}</span>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</div>

<style>
	.bhl { display: flex; flex-direction: column; gap: 14px; }

	/* Tabs (left-aligned underline sub-nav — matches the Build page tabs) */
	.bhl-tabnav { display: flex; border-bottom: 1px solid var(--border); }
	.bhl-tabs { display: flex; gap: 2px; flex-wrap: wrap; }
	.bhl-tab {
		position: relative; display: inline-flex; align-items: center; gap: 7px; padding: 10px 14px;
		border: none; background: transparent;
		color: var(--muted-foreground); font-size: 0.85rem; font-weight: 500; cursor: pointer;
		white-space: nowrap; transition: color 120ms ease;
	}
	.bhl-tab:hover { color: var(--foreground); }
	.bhl-tab.on { color: var(--foreground); }
	.bhl-tab.on::after {
		content: ''; position: absolute; left: 12px; right: 12px; bottom: -1px;
		height: 2px; border-radius: 2px; background: var(--primary);
	}
	.bhl-tab:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 6px; }
	.bhl-tab-count { font-family: var(--font-mono, monospace); font-size: 0.6875rem; color: var(--muted-foreground); }
	.bhl-tab.on .bhl-tab-count { color: var(--foreground); }

	/* Toolbar */
	.bhl-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
	.bhl-search { position: relative; display: flex; align-items: center; flex: 1; max-width: 320px; }
	.bhl-search :global(svg) { position: absolute; left: 11px; color: var(--muted-foreground); pointer-events: none; }
	.bhl-search input {
		width: 100%; height: 34px; padding: 0 12px 0 32px;
		background: var(--input-bg); border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground); font-size: 0.8125rem; outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.bhl-search input::placeholder { color: var(--muted-foreground); }
	.bhl-search input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }
	.bhl-tools { display: flex; align-items: center; gap: 8px; }

	.bhl-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, border-color 120ms ease, filter 120ms ease;
	}
	.bhl-btn:hover { background: var(--hover-overlay); }
	.bhl-btn:disabled { opacity: 0.55; cursor: not-allowed; }
	.bhl-btn--sm { height: 30px; padding: 0 11px; font-size: 0.78rem; }
	.bhl-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.bhl-btn--primary:hover { filter: brightness(1.08); background: var(--primary); }
	.bhl-btn--danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 35%, var(--border)); }
	.bhl-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Toast + error */
	.bhl-toast { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: calc(var(--radius, 0.75rem) - 2px); font-size: 0.8125rem; }
	.bhl-toast.ok { background: color-mix(in oklch, var(--success) 14%, transparent); color: var(--success); border: 1px solid color-mix(in oklch, var(--success) 30%, transparent); }
	.bhl-toast.bad { background: color-mix(in oklch, var(--destructive) 14%, transparent); color: var(--destructive); border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent); }
	.bhl-toast-close { margin-left: auto; background: transparent; border: none; color: inherit; cursor: pointer; padding: 2px; border-radius: 4px; display: grid; place-items: center; opacity: 0.8; }
	.bhl-toast-close:hover { opacity: 1; }
	.bhl-error { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: calc(var(--radius, 0.75rem) - 2px); background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive); font-size: 0.8125rem; }
	.bhl-error-close { margin-left: auto; background: transparent; border: none; color: var(--destructive); cursor: pointer; padding: 2px; border-radius: 4px; display: grid; place-items: center; }

	/* List + rows */
	.bhl-list { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius, 0.75rem); overflow: hidden; background: var(--card); }
	.bhl-row { display: flex; align-items: center; gap: 14px; padding: 13px 16px; min-height: 60px; border-bottom: 1px solid var(--border-subtle, var(--border)); cursor: pointer; transition: background-color 120ms ease; }
	.bhl-row:last-child { border-bottom: none; }
	.bhl-row:hover { background: var(--hover-overlay); }
	.bhl-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.bhl-row--off { opacity: 0.6; }
	.bhl-row--static { cursor: default; }
	.bhl-row--static:hover { background: transparent; }

	.bhl-ic { display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: calc(var(--radius, 0.75rem) - 4px); background: color-mix(in oklch, var(--primary) 12%, transparent); color: var(--primary); }
	.bhl-ic--muted { background: color-mix(in oklch, var(--muted) 50%, transparent); color: var(--muted-foreground); }
	.bhl-body { flex: 1; min-width: 0; }
	.bhl-name { display: flex; align-items: center; gap: 8px; margin: 0 0 3px; font-family: var(--font-display, Georgia, serif); font-size: 0.9rem; font-weight: 700; letter-spacing: -0.01em; color: var(--foreground); overflow: hidden; flex-wrap: wrap; }
	.bhl-badge { flex-shrink: 0; font-size: 0.6rem; font-weight: 600; letter-spacing: 0.03em; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 50%, transparent); padding: 2px 7px; border-radius: 999px; }
	.bhl-badge--mono { font-family: var(--font-mono, monospace); letter-spacing: 0; }
	.bhl-badge--warn { color: var(--warning, #d97706); background: color-mix(in oklch, var(--warning, #d97706) 14%, transparent); }
	.bhl-desc { margin: 0; font-size: 0.78rem; color: var(--muted-foreground); line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.bhl-desc.mono { font-family: var(--font-mono, monospace); font-size: 0.72rem; }

	.bhl-meta { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
	.bhl-tag { font-size: 0.6875rem; font-weight: 500; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 45%, transparent); padding: 2px 8px; border-radius: 6px; white-space: nowrap; }

	.bhl-actions { display: flex; gap: 2px; flex-shrink: 0; opacity: 0; transition: opacity 120ms ease; }
	.bhl-row:hover .bhl-actions, .bhl-row:focus-within .bhl-actions { opacity: 1; }
	.bhl-act { display: grid; place-items: center; width: 28px; height: 28px; background: transparent; border: 1px solid transparent; border-radius: 7px; color: var(--muted-foreground); cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.bhl-act:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bhl-act:disabled { opacity: 0.5; cursor: not-allowed; }
	.bhl-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Enable/disable toggle (always visible) */
	.bhl-toggle { position: relative; width: 36px; height: 20px; border-radius: 10px; border: none; background: var(--muted); cursor: pointer; flex-shrink: 0; transition: background 200ms ease; }
	.bhl-toggle.on { background: var(--success, var(--primary)); }
	.bhl-toggle:disabled { opacity: 0.5; cursor: not-allowed; }
	.bhl-toggle-thumb { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: var(--foreground); transition: transform 200ms ease; }
	.bhl-toggle.on .bhl-toggle-thumb { transform: translateX(16px); background: var(--primary-foreground); }

	/* Inline confirm row */
	.bhl-row--confirm { justify-content: space-between; cursor: default; background: color-mix(in oklch, var(--destructive) 6%, var(--card)); }
	.bhl-row--confirm:hover { background: color-mix(in oklch, var(--destructive) 6%, var(--card)); }
	.bhl-confirm-text { margin: 0; font-size: 0.8rem; color: var(--foreground); }
	.bhl-confirm-actions { display: flex; gap: 8px; flex-shrink: 0; }

	/* New row */
	.bhl-new { display: flex; align-items: center; justify-content: center; gap: 8px; padding: 13px 16px; min-height: 52px; background: transparent; border: none; border-top: 1px dashed color-mix(in oklch, var(--muted-foreground) 35%, transparent); color: var(--muted-foreground); font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.bhl-new:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bhl-new-plus { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: var(--primary); color: var(--primary-foreground); }

	/* Empty + loading */
	.bhl-empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 48px 24px; text-align: center; color: var(--muted-foreground); }
	.bhl-empty-title { margin: 4px 0 0; font-size: 0.875rem; color: var(--foreground); }
	.bhl-empty-sub { margin: 0; font-size: 0.78rem; }
	.bhl-link { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.8125rem; margin-top: 4px; }
	.bhl-link:hover { text-decoration: underline; }
	.bhl-loading { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 48px; color: var(--muted-foreground); }
	.bhl-loading :global(svg) { color: var(--primary); }

	:global(.bhl-spin) { animation: bhl-spin 1s linear infinite; }
	@keyframes bhl-spin { to { transform: rotate(360deg); } }

	@media (max-width: 639px) {
		.bhl-search { max-width: none; }
		.bhl-meta { display: none; }
		.bhl-actions { opacity: 1; }
	}
</style>
