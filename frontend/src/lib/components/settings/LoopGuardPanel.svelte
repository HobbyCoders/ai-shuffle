<script lang="ts">
	/**
	 * LoopGuardPanel — the subscription loop guard in Settings → Providers.
	 *
	 * While Claude Code runs on the user's subscription, turns that start
	 * without them (automations, wake-ups, background workers, connectors)
	 * count against an hourly budget; past it the next such turn waits here for
	 * Confirm or Dismiss. Background workers also get a default time limit.
	 * The guard does nothing in API key mode.
	 */
	import { onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		confirmHeldTurn,
		dismissHeldTurn,
		getLoopGuard,
		updateLoopGuard,
		type LoopGuardSettings,
		type LoopGuardStatus
	} from '$lib/api/loop-guard';

	interface Props {
		isAdmin?: boolean;
	}

	let { isAdmin = false }: Props = $props();

	let status = $state<LoopGuardStatus | null>(null);
	let loadError = $state('');
	let saveError = $state('');
	let busy = $state(false);

	const SOURCE_LABELS: Record<string, string> = {
		automation: 'Automation',
		wakeup: 'Scheduled wake-up',
		provider_agent: 'Background worker',
		connector: 'Connector'
	};

	async function load() {
		try {
			status = await getLoopGuard();
			loadError = '';
		} catch (e) {
			loadError = getErrorMessage(e, 'Could not read the loop guard settings.');
		}
	}

	async function save(patch: Partial<LoopGuardSettings>) {
		if (!isAdmin || busy) return;
		busy = true;
		saveError = '';
		try {
			status = await updateLoopGuard(patch);
		} catch (e) {
			saveError = getErrorMessage(e, 'Could not save the loop guard settings.');
			await load();
		} finally {
			busy = false;
		}
	}

	function saveNumber(field: keyof LoopGuardSettings, raw: string) {
		const value = Number.parseInt(raw, 10);
		if (Number.isNaN(value)) return;
		void save({ [field]: value });
	}

	async function act(id: string, action: 'confirm' | 'dismiss') {
		if (busy) return;
		busy = true;
		saveError = '';
		try {
			if (action === 'confirm') await confirmHeldTurn(id);
			else await dismissHeldTurn(id);
		} catch (e) {
			saveError = getErrorMessage(e, 'Could not update the held turn.');
		} finally {
			busy = false;
			await load();
		}
	}

	function when(seconds: number): string {
		return new Date(seconds * 1000).toLocaleString();
	}

	onMount(() => {
		void load();
	});
</script>

<div class="guard-panel" data-testid="loop-guard-panel">
	{#if !status}
		<p class="hint pad">{loadError || 'Loading…'}</p>
	{:else}
		<p class="hint pad" data-testid="loop-guard-applies">
			{#if !status.enabled}
				The loop guard is off.
			{:else if status.applies}
				Active: Claude Code runs on your subscription. Unattended turns this hour: {status.usage.install_last_hour} of {status.install_turns_per_hour}.
			{:else}
				Not active in API key mode; your Anthropic Console spend limit is the control there.
			{/if}
			It never stops a turn you started yourself.
		</p>

		<!-- Mirrors app/core/loop_guard.py; keep the two in step. -->
		<details class="explain pad" data-testid="loop-guard-explainer">
			<summary>What the loop guard counts and why</summary>
			<p>
				On a Claude subscription, every agent run uses your plan's limits. A runaway automation, an
				agent that keeps scheduling wake-ups, a flood of connector messages or a crowd of background
				workers could use them up while nobody is watching.
			</p>
			<ul>
				<li>
					<strong>Counted:</strong> turns that start without you (automations, scheduled wake-ups,
					background workers and connector messages) when they run on the Claude Code CLI in subscription
					mode. Each one counts against its session and against this worker over the last hour. Defaults:
					{status.defaults.session_turns_per_hour} per session, {status.defaults.install_turns_per_hour} per worker.
				</li>
				<li>
					<strong>When a budget is used up:</strong> the next unattended turn does not start. The session
					shows why, and the turn waits under "Waiting for confirmation" here. Confirm starts it now (it
					still counts toward the hour); Dismiss drops it. A held turn never runs by itself; new unattended
					turns start again once the last hour's count is back under the budget.
				</li>
				<li>
					<strong>Worker time limit:</strong> a background worker on the Claude Code CLI stops after
					{status.defaults.worker_max_minutes} minutes by default when nothing else set a limit.
				</li>
				<li>
					<strong>Not covered:</strong> turns you start yourself (chat, queued messages, resumes, API
					requests), API key mode (your Anthropic Console spend limit is the control there), and turns that
					run on another provider. Counts are kept in memory and start from zero when the worker restarts.
				</li>
			</ul>
		</details>

		<div class="set-row">
			<div class="lab"><h4>Loop guard</h4><p class="hint">Hold unattended turns past the budget until you confirm them.</p></div>
			<div class="ctl">
				<input
					type="checkbox"
					aria-label="Loop guard enabled"
					checked={status.enabled}
					disabled={!isAdmin || busy}
					onchange={(e) => save({ enabled: e.currentTarget.checked })}
				/>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Per session, per hour</h4><p class="hint">Default {status.defaults.session_turns_per_hour}</p></div>
			<div class="ctl">
				<input class="inp num" type="number" min="1" aria-label="Unattended turns per session per hour"
					value={status.session_turns_per_hour} disabled={!isAdmin || busy}
					onchange={(e) => saveNumber('session_turns_per_hour', e.currentTarget.value)} />
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Per worker, per hour</h4><p class="hint">Default {status.defaults.install_turns_per_hour}</p></div>
			<div class="ctl">
				<input class="inp num" type="number" min="1" aria-label="Unattended turns per worker per hour"
					value={status.install_turns_per_hour} disabled={!isAdmin || busy}
					onchange={(e) => saveNumber('install_turns_per_hour', e.currentTarget.value)} />
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Background worker time limit (minutes)</h4><p class="hint">Default {status.defaults.worker_max_minutes}; 0 turns it off</p></div>
			<div class="ctl">
				<input class="inp num" type="number" min="0" aria-label="Background worker time limit in minutes"
					value={status.worker_max_minutes} disabled={!isAdmin || busy}
					onchange={(e) => saveNumber('worker_max_minutes', e.currentTarget.value)} />
			</div>
		</div>

		{#if status.held.length}
			<div class="held" data-testid="loop-guard-held">
				<h4 class="pad">Waiting for confirmation</h4>
				{#if isAdmin}<p class="hint pad">Confirm starts the turn now; it still counts toward the hour. Dismiss drops it.</p>{/if}
				{#each status.held as item (item.id)}
					<div class="held-item">
						<div class="held-text">
							<span class="held-title">{SOURCE_LABELS[item.source] ?? item.source} · {when(item.held_at)}</span>
							{#if item.prompt}<span class="held-prompt">{item.prompt}</span>{/if}
						</div>
						{#if isAdmin}
							<div class="held-actions">
								<button type="button" class="ed-btn primary" disabled={busy} onclick={() => act(item.id, 'confirm')}>Confirm</button>
								<button type="button" class="ed-btn" disabled={busy} onclick={() => act(item.id, 'dismiss')}>Dismiss</button>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
		{#if saveError}<p class="msg err pad" role="alert">{saveError}</p>{/if}
	{/if}
</div>

<style>
	.guard-panel { display: grid; gap: 6px; padding: 4px 0; }
	.hint { margin: 0; color: var(--muted-foreground); font-size: 12px; line-height: 1.5; }
	.pad { padding: 0 14px; }
	.num { width: 90px; }
	.explain { font-size: 12px; line-height: 1.5; color: var(--muted-foreground); }
	.explain summary { cursor: pointer; color: var(--foreground); }
	.explain summary:focus-visible { outline: 2px solid var(--ring, var(--primary)); outline-offset: 2px; border-radius: 3px; }
	.explain p, .explain ul { margin: 6px 0 0; }
	.explain ul { padding-left: 18px; display: grid; gap: 4px; }
	.explain strong { color: var(--foreground); font-weight: 600; }
	.held { display: grid; gap: 6px; }
	.held h4 { margin: 4px 0 0; font-size: 13px; }
	.held-item { display: flex; gap: 10px; align-items: center; justify-content: space-between; margin: 0 14px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; }
	.held-text { display: grid; gap: 2px; min-width: 0; }
	.held-title { font-size: 12.5px; font-weight: 600; }
	.held-prompt { font-size: 12px; color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.held-actions { display: flex; gap: 6px; flex-shrink: 0; }
	.msg { margin: 0; font-size: 12.5px; }
	.msg.err { color: var(--destructive); }
</style>
