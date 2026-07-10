<script lang="ts">
	/**
	 * AutomationsStudio — the Studio-v2 Automations surface.
	 *
	 * Matches the mockup `#page-automations`: a Studio page header (eyebrow + serif
	 * title + count subline + "New automation"), an approval banner per pending run,
	 * and a `.lrow`-style run list (trigger icon + name/status + trigger/profile/cost
	 * meta). Reuses AutomationsCard's data + handlers; only the list view is new.
	 * Theme tokens only — never the mockup palette.
	 */
	import type { Automation, AutomationApproval, TriggerType } from '$lib/api/automations';
	import type { Profile } from '$lib/api/client';
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import {
		Search, Plus, Clock, Activity, Globe, Webhook, CalendarClock,
		Play, Pencil, Trash2, AlertCircle, AlertTriangle, X, Loader2
	} from 'lucide-svelte';

	interface Props {
		automations: Automation[];
		pendingApprovals: AutomationApproval[];
		profiles: Profile[];
		loading?: boolean;
		error?: string;
		triggeringId?: string | null;
		onOpenCreate: () => void;
		onOpenDetail: (automation: Automation) => void;
		onOpenEdit: (automation: Automation) => void;
		onToggleEnabled: (automation: Automation) => void;
		onTrigger: (id: string) => void;
		onDelete: (id: string) => void;
		onApprove: (approvalId: string) => void;
		onDeny: (approvalId: string) => void;
		onClearError: () => void;
		formatTime: (dateStr: string | null) => string;
		formatCost: (cost: number) => string;
		triggerTypeLabel: (type: TriggerType) => string;
	}

	let {
		automations,
		pendingApprovals,
		profiles,
		loading = false,
		error = '',
		triggeringId = null,
		onOpenCreate,
		onOpenDetail,
		onOpenEdit,
		onToggleEnabled,
		onTrigger,
		onDelete,
		onApprove,
		onDeny,
		onClearError,
		formatTime,
		formatCost,
		triggerTypeLabel
	}: Props = $props();

	let searchQuery = $state('');
	let pendingDelete = $state<string | null>(null);
	// Touch/narrow only: which row has its collapsed ⋮ action tray expanded.
	// One open at a time; desktop keeps the always-inline hover-revealed rail.
	let actionsOpenId = $state<string | null>(null);

	// The ⋮ toggle expands/collapses a row's action tray. The toggle and every
	// tray button stop propagation, so any click reaching the window is outside an
	// open tray — collapse it and disarm any pending delete confirmation.
	function toggleRowActions(e: MouseEvent | KeyboardEvent, id: string) {
		e.stopPropagation();
		actionsOpenId = actionsOpenId === id ? null : id;
	}
	function onWindowClick() {
		if (actionsOpenId !== null) actionsOpenId = null;
		if (pendingDelete !== null) pendingDelete = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			actionsOpenId = null;
			pendingDelete = null;
		}
	}

	const profileNames = $derived(new Map(profiles.map((p) => [p.id, p.name])));
	const enabledCount = $derived(automations.filter((a) => a.is_enabled).length);

	const summary = $derived(
		`${automations.length} ${automations.length === 1 ? 'automation' : 'automations'}` +
		` · ${enabledCount} active` +
		(pendingApprovals.length > 0 ? ` · ${pendingApprovals.length} awaiting approval` : '')
	);

	const filtered = $derived.by(() => {
		const q = searchQuery.trim().toLowerCase();
		if (!q) return automations;
		return automations.filter(
			(a) =>
				a.name.toLowerCase().includes(q) ||
				a.trigger_type.toLowerCase().includes(q) ||
				(a.triggers || []).some((t) => t.type.toLowerCase().includes(q)) ||
				(a.description?.toLowerCase().includes(q) ?? false)
		);
	});

	// Icon by primary trigger type.
	function triggerIcon(a: Automation) {
		const type = a.triggers?.[0]?.type ?? a.trigger_type;
		if (type === 'webhook') return Webhook;
		if (type === 'connector_event' || type === 'connector_trigger') return Activity;
		if (type === 'cron' || type === 'interval') return Clock;
		return CalendarClock;
	}

	function triggerTags(a: Automation): string[] {
		const list = (a.triggers && a.triggers.length > 0)
			? a.triggers.map((t) => t.type)
			: [a.trigger_type];
		const tags = list.slice(0, 2).map((t) => triggerTypeLabel(t));
		if (list.length > 2) tags.push(`+${list.length - 2}`);
		return tags;
	}

	function rowDesc(a: Automation): string {
		if (a.description) return a.description;
		if (a.prompt) return a.prompt;
		return '';
	}

	// Status dot class: off (disabled) · run (running) · err (error) · ok (idle).
	function dotClass(a: Automation): string {
		if (!a.is_enabled) return 'off';
		if (a.status === 'running') return 'run';
		if (a.status === 'error' || a.last_error) return 'err';
		return 'ok';
	}

	// Rightmost status text + tone.
	function statInfo(a: Automation): { text: string; tone: string } {
		if (!a.is_enabled) return { text: 'disabled', tone: 'muted' };
		if (a.status === 'running') return { text: 'running', tone: 'live' };
		if (a.status === 'error' || a.last_error) return { text: 'last: error', tone: 'warn' };
		if (!a.last_run_at) return { text: 'never run', tone: 'muted' };
		return { text: formatCost(a.total_cost_used), tone: 'normal' };
	}

	function confirmDelete(id: string) {
		pendingDelete = null;
		onDelete(id);
	}
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<div class="as">
	<div class="as-wrap">
		<div class="as-head">
			<StudioPageHeader eyebrow="Scheduled & triggered runs" title="Automations">
				{#snippet actions()}
					<button class="as-btn as-btn--primary" onclick={onOpenCreate}>
						<Plus size={15} strokeWidth={2} /><span>New automation</span>
					</button>
				{/snippet}
			</StudioPageHeader>
			<p class="as-sub">{summary}</p>
		</div>

		<div class="as-scroll">
			{#if error}
				<div class="as-error">
					<AlertCircle size={15} strokeWidth={1.7} />
					<span>{error}</span>
					<button class="as-error-close" onclick={onClearError} aria-label="Dismiss"><X size={14} strokeWidth={2} /></button>
				</div>
			{/if}

			<!-- Approval banners (one per pending run) -->
			{#each pendingApprovals as approval (approval.id)}
				<div class="as-approval">
					<span class="as-approval-ic"><AlertTriangle size={19} strokeWidth={1.9} /></span>
					<div class="as-approval-body">
						<h4>Approval needed · {approval.automation_name || 'Automation'}</h4>
						<p>{approval.prompt_preview || 'Review the rendered prompt before this run starts.'}</p>
					</div>
					<div class="as-approval-acts">
						<button class="as-btn as-btn--sm" onclick={() => onDeny(approval.id)}>Deny</button>
						<button class="as-btn as-btn--sm as-btn--primary" onclick={() => onApprove(approval.id)}>Approve</button>
					</div>
				</div>
			{/each}

			<!-- Toolbar: search left -->
			<div class="as-toolbar">
				<div class="as-search">
					<Search size={14} strokeWidth={1.7} />
					<input type="text" placeholder="Search automations…" autocomplete="off" bind:value={searchQuery} />
				</div>
			</div>

			{#if loading}
				<div class="as-loading"><Loader2 size={28} class="as-spin" /><p>Loading automations…</p></div>
			{:else if filtered.length === 0}
				<div class="as-empty">
					<CalendarClock size={34} strokeWidth={1.25} />
					<p class="as-empty-title">{searchQuery.trim() ? 'No automations match your search' : 'No automations yet'}</p>
					{#if !searchQuery.trim()}
						<p class="as-empty-sub">Run AI tasks on a schedule or in response to connector events</p>
						<button class="as-link" onclick={onOpenCreate}>Create your first automation</button>
					{/if}
				</div>
			{:else}
				<div class="as-list">
					{#each filtered as a (a.id)}
						{#if pendingDelete === a.id}
							<div class="as-row as-row--confirm">
								<p class="as-confirm-text">Delete <strong>{a.name}</strong>? This can't be undone.</p>
								<div class="as-confirm-actions">
									<button class="as-btn as-btn--sm" onclick={() => (pendingDelete = null)}>Cancel</button>
									<button class="as-btn as-btn--sm as-btn--danger" onclick={() => confirmDelete(a.id)}>Delete</button>
								</div>
							</div>
						{:else}
							{@const TrigIcon = triggerIcon(a)}
							{@const stat = statInfo(a)}
							<div
								class="as-row"
								class:as-row--off={!a.is_enabled}
								role="button"
								tabindex="0"
								onclick={() => onOpenDetail(a)}
								onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpenDetail(a); } }}
							>
								<span class="as-ic"><TrigIcon size={17} strokeWidth={1.7} /></span>
								<div class="as-body">
									<h4 class="as-name">
										<span class="as-sd as-sd--{dotClass(a)}" title={a.status}></span>
										{a.name}
										{#if !a.is_enabled}<span class="as-badge as-badge--warn">Disabled</span>{/if}
									</h4>
									{#if rowDesc(a)}<p class="as-desc">{rowDesc(a)}</p>{/if}
								</div>
								<div class="as-meta">
									{#each triggerTags(a) as tag}<span class="as-tag">{tag}</span>{/each}
									{#if profileNames.get(a.profile_id)}<span class="as-tag">{profileNames.get(a.profile_id)}</span>{/if}
									<span class="as-stat as-stat--{stat.tone}">{stat.text}</span>
								</div>
								<RowActionMenu
									open={actionsOpenId === a.id}
									onToggle={(e) => toggleRowActions(e, a.id)}
									label="Automation actions"
								>
									<button class="as-act" title="Run now" onclick={(e) => { e.stopPropagation(); actionsOpenId = null; onTrigger(a.id); }} disabled={triggeringId === a.id}>
										{#if triggeringId === a.id}<Loader2 size={15} class="as-spin" />{:else}<Play size={15} strokeWidth={1.7} />{/if}
									</button>
									<button class="as-act" title="Edit" onclick={(e) => { e.stopPropagation(); onOpenEdit(a); }}><Pencil size={15} strokeWidth={1.7} /></button>
									<button class="as-act as-act--danger" title="Delete" onclick={(e) => { e.stopPropagation(); actionsOpenId = null; pendingDelete = a.id; }}><Trash2 size={15} strokeWidth={1.7} /></button>
								</RowActionMenu>
								<button
									class="as-toggle"
									class:on={a.is_enabled}
									title={a.is_enabled ? 'Disable automation' : 'Enable automation'}
									aria-label={a.is_enabled ? 'Disable automation' : 'Enable automation'}
									onclick={(e) => { e.stopPropagation(); onToggleEnabled(a); }}
								>
									<span class="as-toggle-thumb"></span>
								</button>
							</div>
						{/if}
					{/each}

					<button class="as-new" onclick={onOpenCreate}>
						<span class="as-new-plus"><Plus size={16} strokeWidth={2.2} /></span>
						<span>New automation</span>
					</button>
				</div>
			{/if}
		</div>
	</div>
</div>

<style>
	.as {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}
	/* Centered, max-width container — matches the mockup's .wrap (not full-bleed). */
	.as-wrap {
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.as-head { flex-shrink: 0; padding: 22px 28px 0; }
	.as-sub { margin: 8px 0 0; font-size: 0.8125rem; color: var(--muted-foreground); }

	.as-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 18px 28px 28px; display: flex; flex-direction: column; gap: 14px; }

	/* Error */
	.as-error { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: calc(var(--radius, 0.75rem) - 2px); background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive); font-size: 0.8125rem; }
	.as-error-close { margin-left: auto; background: transparent; border: none; color: var(--destructive); cursor: pointer; padding: 2px; display: grid; place-items: center; }

	/* Approval banner */
	.as-approval {
		display: flex; align-items: center; gap: 14px; padding: 14px 16px;
		border: 1px solid color-mix(in oklch, var(--warning) 40%, var(--border));
		border-radius: var(--radius, 0.75rem);
		background: color-mix(in oklch, var(--warning) 12%, var(--card));
	}
	.as-approval-ic { width: 38px; height: 38px; border-radius: 50%; background: var(--warning); display: grid; place-items: center; color: #fff; flex-shrink: 0; }
	.as-approval-body { min-width: 0; flex: 1; }
	.as-approval-body h4 { margin: 0 0 2px; font-family: var(--font-display, Georgia, serif); font-weight: 700; font-size: 0.95rem; color: var(--foreground); }
	.as-approval-body p { margin: 0; font-size: 0.8rem; color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.as-approval-acts { margin-left: auto; display: flex; gap: 8px; flex-shrink: 0; }

	/* Toolbar */
	.as-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
	.as-search { position: relative; display: flex; align-items: center; flex: 1; max-width: 320px; }
	.as-search :global(svg) { position: absolute; left: 11px; color: var(--muted-foreground); pointer-events: none; }
	.as-search input {
		width: 100%; height: 34px; padding: 0 12px 0 32px;
		background: var(--input-bg); border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground); font-size: 0.8125rem; outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.as-search input::placeholder { color: var(--muted-foreground); }
	.as-search input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }

	/* Buttons */
	.as-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, border-color 120ms ease, filter 120ms ease;
	}
	.as-btn:hover { background: var(--hover-overlay); }
	.as-btn:disabled { opacity: 0.55; cursor: not-allowed; }
	.as-btn--sm { height: 30px; padding: 0 11px; font-size: 0.78rem; }
	.as-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.as-btn--primary:hover { filter: brightness(1.08); background: var(--primary); }
	.as-btn--danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 35%, var(--border)); }
	.as-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* List + rows */
	.as-list { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius, 0.75rem); overflow: hidden; background: var(--card); }
	.as-row { display: flex; align-items: center; gap: 14px; padding: 13px 16px; min-height: 60px; border-bottom: 1px solid var(--border-subtle, var(--border)); cursor: pointer; transition: background-color 120ms ease; }
	.as-row:last-child { border-bottom: none; }
	.as-row:hover { background: var(--hover-overlay); }
	.as-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.as-row--off { opacity: 0.62; }

	.as-ic { display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: calc(var(--radius, 0.75rem) - 4px); background: color-mix(in oklch, var(--primary) 12%, transparent); color: var(--primary); }
	.as-body { flex: 1; min-width: 0; }
	.as-name { display: flex; align-items: center; gap: 8px; margin: 0 0 3px; font-family: var(--font-display, Georgia, serif); font-size: 0.9rem; font-weight: 700; letter-spacing: -0.01em; color: var(--foreground); overflow: hidden; flex-wrap: wrap; }
	.as-desc { margin: 0; font-size: 0.78rem; color: var(--muted-foreground); line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

	/* status dot */
	.as-sd { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; display: inline-block; }
	.as-sd--ok { background: var(--success, #16a34a); }
	.as-sd--run { background: var(--primary); }
	.as-sd--err { background: var(--destructive); }
	.as-sd--off { background: var(--muted-foreground); }

	.as-badge { flex-shrink: 0; font-size: 0.6rem; font-weight: 600; letter-spacing: 0.03em; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 50%, transparent); padding: 2px 7px; border-radius: 999px; }
	.as-badge--warn { color: var(--warning, #d97706); background: color-mix(in oklch, var(--warning, #d97706) 14%, transparent); }

	.as-meta { display: flex; align-items: center; gap: 8px; flex-shrink: 0; font-family: var(--font-mono, monospace); }
	.as-tag { font-size: 0.6875rem; font-weight: 500; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 45%, transparent); padding: 2px 8px; border-radius: 6px; white-space: nowrap; }
	.as-stat { font-size: 0.6875rem; white-space: nowrap; color: var(--muted-foreground); }
	.as-stat--live { color: var(--primary); }
	.as-stat--warn { color: var(--warning, #d97706); }
	.as-stat--normal { color: var(--foreground); }

	/* Row-hover reveal of the action rail is signalled to RowActionMenu through the
	   inherited --ram-reveal custom property (0 hidden / 1 shown); the component
	   owns the button opacity + the touch/narrow ⋮ collapse + slide-out. */
	.as-row { --ram-reveal: 0; }
	.as-row:hover, .as-row:focus-within { --ram-reveal: 1; }
	.as-act { display: grid; place-items: center; width: 28px; height: 28px; background: transparent; border: 1px solid transparent; border-radius: 7px; color: var(--muted-foreground); cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.as-act:hover { color: var(--foreground); background: var(--hover-overlay); }
	.as-act:disabled { opacity: 0.5; cursor: not-allowed; }
	.as-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Enable/disable toggle (always visible) */
	.as-toggle { position: relative; width: 36px; height: 20px; border-radius: 10px; border: none; background: var(--muted); cursor: pointer; flex-shrink: 0; transition: background 200ms ease; }
	.as-toggle.on { background: var(--success, var(--primary)); }
	.as-toggle-thumb { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: var(--foreground); transition: transform 200ms ease; }
	.as-toggle.on .as-toggle-thumb { transform: translateX(16px); background: var(--primary-foreground); }

	/* Inline confirm row */
	.as-row--confirm { justify-content: space-between; cursor: default; background: color-mix(in oklch, var(--destructive) 6%, var(--card)); }
	.as-row--confirm:hover { background: color-mix(in oklch, var(--destructive) 6%, var(--card)); }
	.as-confirm-text { margin: 0; font-size: 0.8rem; color: var(--foreground); }
	.as-confirm-actions { display: flex; gap: 8px; flex-shrink: 0; }

	/* New row */
	.as-new { display: flex; align-items: center; justify-content: center; gap: 8px; padding: 13px 16px; min-height: 52px; background: transparent; border: none; border-top: 1px dashed color-mix(in oklch, var(--muted-foreground) 35%, transparent); color: var(--muted-foreground); font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.as-new:hover { color: var(--foreground); background: var(--hover-overlay); }
	.as-new-plus { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: var(--primary); color: var(--primary-foreground); }

	/* Empty + loading */
	.as-empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 48px 24px; text-align: center; color: var(--muted-foreground); }
	.as-empty-title { margin: 4px 0 0; font-size: 0.875rem; color: var(--foreground); }
	.as-empty-sub { margin: 0; font-size: 0.78rem; }
	.as-link { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.8125rem; margin-top: 4px; }
	.as-link:hover { text-decoration: underline; }
	.as-loading { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 48px; color: var(--muted-foreground); }
	.as-loading :global(svg) { color: var(--primary); }

	:global(.as-spin) { animation: as-spin 1s linear infinite; }
	@keyframes as-spin { to { transform: rotate(360deg); } }

	@media (max-width: 639px) {
		.as-head { padding: 18px 16px 0; }
		.as-scroll { padding: 14px 16px 20px; }
		.as-search { max-width: none; }
		.as-meta { display: none; }
		.as-approval { flex-wrap: wrap; }
		.as-approval-acts { margin-left: 0; width: 100%; }
		.as-approval-acts .as-btn { flex: 1; justify-content: center; }
	}
</style>
