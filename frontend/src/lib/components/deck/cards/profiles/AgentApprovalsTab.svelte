<script lang="ts">
	/**
	 * The Approvals tab: the agent's saved approvals, the permanent allow or
	 * deny rules a permission prompt saves when the owner picks "Always for
	 * this agent". Each row names the tool, what it covers (every call, or a
	 * pattern), the decision and the day it was saved, with Remove; Remove all
	 * clears the lot. Removing has no undo, so each asks first. Owner only.
	 */
	import { untrack } from 'svelte';
	import { Check, RotateCw, ShieldCheck, X } from 'lucide-svelte';
	import RowGroup from '$lib/components/settings/rows/RowGroup.svelte';
	import InlineConfirm from '$lib/components/settings/rows/InlineConfirm.svelte';
	import InlineBanner from '$lib/components/settings/rows/InlineBanner.svelte';
	import {
		deleteAllPermissionRules,
		deletePermissionRule,
		listPermissionRules,
		ruleErrorText,
		type PermissionRule
	} from '$lib/api/permissionRules';

	interface Props {
		/** null while the agent is an unsaved draft. */
		profileId: string | null;
	}

	let { profileId }: Props = $props();

	let rules = $state<PermissionRule[]>([]);
	let loading = $state(false);
	let loadError = $state('');
	let bulkError = $state('');
	/** Per-row remove failure, keyed by rule id. */
	let rowErrors = $state<Record<string, string>>({});
	let removeAllArmed = $state(false);
	/** Bumped on every load so a slow answer for an earlier agent is dropped. */
	let loadSeq = 0;

	async function load(id: string) {
		const seq = ++loadSeq;
		loading = true;
		loadError = '';
		bulkError = '';
		rowErrors = {};
		try {
			const next = await listPermissionRules(id);
			if (seq !== loadSeq) return;
			rules = next;
		} catch (err) {
			if (seq !== loadSeq) return;
			rules = [];
			loadError = ruleErrorText(err, 'Could not load saved approvals.');
		} finally {
			if (seq === loadSeq) loading = false;
		}
	}

	$effect(() => {
		const id = profileId;
		untrack(() => {
			rules = [];
			removeAllArmed = false;
			if (id) void load(id);
			else {
				loadSeq++;
				loading = false;
				loadError = '';
			}
		});
	});

	function isNotFound(err: unknown): boolean {
		return (err as { status?: unknown } | null)?.status === 404;
	}

	async function removeOne(rule: PermissionRule) {
		if (!profileId) return;
		const { [rule.id]: _cleared, ...rest } = rowErrors;
		rowErrors = rest;
		try {
			await deletePermissionRule(profileId, rule.id);
		} catch (err) {
			// Already gone (removed on another worker): drop the row all the same.
			if (!isNotFound(err) || ruleErrorText(err, '') !== 'Permission rule not found') {
				rowErrors = { ...rowErrors, [rule.id]: ruleErrorText(err, 'Could not remove this rule.') };
				return;
			}
		}
		rules = rules.filter((r) => r.id !== rule.id);
	}

	async function removeAll() {
		if (!profileId) return;
		bulkError = '';
		try {
			await deleteAllPermissionRules(profileId);
			rules = [];
			rowErrors = {};
		} catch (err) {
			bulkError = ruleErrorText(err, 'Could not remove the saved approvals.');
		}
	}

	function savedOn(at: string): string {
		const ms = Date.parse(at);
		if (!Number.isFinite(ms)) return '';
		return new Date(ms).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
	}
</script>

{#if !profileId}
	<p class="st-empty">Save this agent to see its saved approvals.</p>
{:else}
	<RowGroup title="Saved approvals" count={!loading && !loadError && rules.length ? rules.length : undefined}>
		{#snippet actions()}
			{#if !loading && !loadError && rules.length > 1}
				<InlineConfirm
					bind:armed={removeAllArmed}
					label="Remove all"
					question={`Remove all ${rules.length}?`}
					variant="ghost"
					size="sm"
					onConfirm={removeAll}
				/>
			{/if}
		{/snippet}
		{#if loading}
			<div class="apr-skel" role="status" aria-label="Loading saved approvals">
				{#each [0, 1] as i (i)}
					<div class="apr-skel-row" style="--d: {i * 120}ms">
						<span class="sk pill"></span>
						<span class="sk-lines"><span class="sk a"></span><span class="sk b"></span></span>
					</div>
				{/each}
			</div>
		{:else if loadError}
			<div class="apr-fail" role="alert">
				<p>{loadError}</p>
				<button type="button" class="st-btn sm" onclick={() => profileId && load(profileId)}><RotateCw />Try again</button>
			</div>
		{:else if rules.length === 0}
			<div class="apr-empty">
				<span class="apr-empty-ico" aria-hidden="true"><ShieldCheck /></span>
				<div>
					<p class="t">No saved approvals</p>
					<p class="h">When you answer a prompt with "Always for this agent", the rule appears here so you can remove it.</p>
				</div>
			</div>
		{:else}
			<ul class="apr-list" aria-label="Saved approvals">
				{#each rules as rule (rule.id)}
					<li class="apr-row" data-decision={rule.decision}>
						<span class="apr-pill {rule.decision}">
							{#if rule.decision === 'allow'}<Check aria-hidden="true" />Allow{:else}<X aria-hidden="true" />Deny{/if}
						</span>
						<div class="apr-main">
							<span class="apr-tool">{rule.tool_name}</span>
							<span class="apr-meta">
								{#if rule.tool_pattern}<code class="apr-pattern" title={rule.tool_pattern}>{rule.tool_pattern}</code>{:else}<span>Every call</span>{/if}
								{#if savedOn(rule.created_at)}<span class="dot" aria-hidden="true">·</span><span>Saved {savedOn(rule.created_at)}</span>{/if}
							</span>
							{#if rowErrors[rule.id]}<span class="apr-err" role="alert">{rowErrors[rule.id]}</span>{/if}
						</div>
						<div class="apr-act">
							<InlineConfirm
								label="Remove"
								question="Remove this rule?"
								size="sm"
								variant="ghost"
								data-rule={rule.id}
								onConfirm={() => removeOne(rule)}
							/>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</RowGroup>
	{#if bulkError}
		<InlineBanner>{bulkError}</InlineBanner>
	{/if}
	{#if !loading && !loadError && rules.length > 0}
		<p class="apr-note">A removed rule stops applying on every worker once they sync. The agent asks again next time.</p>
	{/if}
{/if}

<style>
	.apr-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.apr-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 12px;
		min-height: 56px;
		padding: 9px 16px;
		animation: apr-in 0.22s cubic-bezier(0.22, 1, 0.36, 1) both;
	}
	.apr-row + .apr-row {
		border-top: 1px solid var(--st-border);
	}
	@keyframes apr-in {
		from {
			opacity: 0;
			transform: translateY(3px);
		}
	}
	.apr-pill {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		min-width: 72px;
		justify-content: center;
		padding: 3px 9px;
		border-radius: 999px;
		font-size: 12px;
		font-weight: 600;
		line-height: 1.4;
		border: 1px solid transparent;
	}
	.apr-pill :global(svg.lucide) {
		width: 13px;
		height: 13px;
	}
	.apr-pill.allow {
		color: var(--st-success);
		background: color-mix(in oklch, var(--st-success) 12%, transparent);
		border-color: color-mix(in oklch, var(--st-success) 35%, transparent);
	}
	.apr-pill.deny {
		color: var(--st-destructive);
		background: var(--st-destructive-soft);
		border-color: var(--st-destructive-line);
	}
	.apr-main {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.apr-tool {
		font-family: var(--st-mono);
		font-size: 13.5px;
		font-weight: 600;
		color: var(--st-fg);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.apr-meta {
		display: flex;
		align-items: baseline;
		gap: 6px;
		min-width: 0;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.apr-meta > span {
		white-space: nowrap;
	}
	.apr-pattern {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--st-mono);
		font-size: 11.5px;
		padding: 1px 5px;
		border-radius: var(--st-radius-sm);
		background: var(--st-code-bg);
		color: var(--st-fg);
	}
	.dot {
		color: var(--st-subtle);
	}
	.apr-err {
		font-size: 12.5px;
		color: var(--st-destructive);
	}
	.apr-act {
		display: flex;
		justify-content: flex-end;
	}

	.apr-empty {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 16px;
	}
	.apr-empty-ico {
		flex: none;
		display: grid;
		place-items: center;
		width: 34px;
		height: 34px;
		border-radius: var(--st-radius-md);
		background: var(--st-primary-soft);
		color: var(--st-primary);
	}
	.apr-empty-ico :global(svg.lucide) {
		width: 18px;
		height: 18px;
	}
	.apr-empty .t {
		margin: 0;
		font-size: 14px;
		font-weight: 500;
		color: var(--st-fg);
	}
	.apr-empty .h {
		margin: 2px 0 0;
		font-size: 12.5px;
		line-height: 1.45;
		color: var(--st-mfg);
	}

	.apr-fail {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 8px 12px;
		padding: 12px 16px;
	}
	.apr-fail p {
		margin: 0;
		font-size: 13px;
		color: var(--st-destructive);
	}
	.apr-fail :global(svg.lucide) {
		width: 13px;
		height: 13px;
	}

	.apr-skel-row {
		display: flex;
		align-items: center;
		gap: 12px;
		min-height: 56px;
		padding: 9px 16px;
	}
	.apr-skel-row + .apr-skel-row {
		border-top: 1px solid var(--st-border);
	}
	.sk {
		display: block;
		border-radius: 999px;
		background: linear-gradient(90deg, var(--st-muted) 0%, color-mix(in oklch, var(--st-muted) 55%, var(--st-card)) 50%, var(--st-muted) 100%);
		background-size: 200% 100%;
		animation: apr-shimmer 1.3s ease-in-out infinite;
		animation-delay: var(--d, 0ms);
	}
	.sk.pill {
		width: 64px;
		height: 22px;
	}
	.sk-lines {
		display: flex;
		flex-direction: column;
		gap: 6px;
		flex: 1;
	}
	.sk.a {
		width: 30%;
		height: 11px;
	}
	.sk.b {
		width: 48%;
		height: 9px;
	}
	@keyframes apr-shimmer {
		from {
			background-position: 100% 0;
		}
		to {
			background-position: -100% 0;
		}
	}

	.apr-note {
		margin: 8px 2px 0;
		font-size: 12.5px;
		color: var(--st-mfg);
	}

	/* Narrow detail: an open confirm drops under the rule. */
	@container agd (max-width: 560px) {
		.apr-row {
			gap: 8px 10px;
			padding: 10px 14px;
		}
		.apr-row:has(:global(.st-ask)) .apr-act {
			grid-column: 1 / -1;
		}
		.apr-tool {
			white-space: normal;
			overflow-wrap: anywhere;
		}
		.apr-meta {
			flex-direction: column;
			align-items: flex-start;
			gap: 2px;
		}
		.apr-pattern {
			max-width: 100%;
		}
		.dot {
			display: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.apr-row,
		.sk {
			animation: none;
		}
	}
</style>
