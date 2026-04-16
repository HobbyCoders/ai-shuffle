<script lang="ts">
	/**
	 * TeamStatusBar - Compact horizontal bar summarizing active agent teams
	 * for the current session. Polls GET /api/v1/teams/{sessionId} every 2s.
	 */
	import { listTeams, killTeammate, type Team, type Teammate } from '$lib/api/client';

	interface Props {
		sessionId: string;
	}

	let { sessionId }: Props = $props();

	let teams = $state<Team[]>([]);
	let expanded = $state<Record<string, boolean>>({});
	let killingKey = $state<string | null>(null);

	function teammateKey(team: string, member: string): string {
		return `${team}::${member}`;
	}

	async function refresh(id: string) {
		try {
			const next = await listTeams(id);
			teams = Array.isArray(next) ? next : [];
		} catch {
			// silently swallow polling errors
		}
	}

	$effect(() => {
		const id = sessionId;
		if (!id) {
			teams = [];
			return;
		}
		refresh(id);
		const handle = setInterval(() => refresh(id), 2000);
		return () => clearInterval(handle);
	});

	function runningCount(team: Team): number {
		return team.teammates.filter((t) => t.status === 'running').length;
	}

	function toggle(teamName: string) {
		expanded = { ...expanded, [teamName]: !expanded[teamName] };
	}

	async function handleKill(team: Team, mate: Teammate) {
		const key = teammateKey(team.team_name, mate.teammate_name);
		if (killingKey) return;
		killingKey = key;
		try {
			await killTeammate(sessionId, team.team_name, mate.teammate_name);
			await refresh(sessionId);
		} catch {
			// ignore; next poll will reconcile
		} finally {
			killingKey = null;
		}
	}

	function formatTokens(n?: number): string | null {
		if (!n) return null;
		return n >= 1000 ? `${(n / 1000).toFixed(0)}k` : String(n);
	}

	function statusPillClass(s: Teammate['status']): string {
		switch (s) {
			case 'running':
				return 'bg-primary/20 text-primary';
			case 'completed':
				return 'bg-green-500/20 text-green-600';
			case 'error':
				return 'bg-red-500/20 text-red-600';
			case 'killed':
				return 'bg-amber-500/20 text-amber-600';
		}
	}
</script>

{#if teams.length > 0}
	<div class="sticky top-0 z-10 bg-muted/30 border-b border-border backdrop-blur-sm">
		<div class="flex items-center gap-2 overflow-x-auto px-2 sm:px-4 py-1.5 sm:py-2 no-scrollbar">
			<!-- Teams icon -->
			<svg class="w-4 h-4 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
			</svg>

			{#each teams as team (team.team_name)}
				{@const running = runningCount(team)}
				{@const isOpen = expanded[team.team_name] === true}
				<button
					type="button"
					onclick={() => toggle(team.team_name)}
					class="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-primary/10 text-primary border border-primary/20 hover:bg-primary/20 transition-colors flex-shrink-0 min-h-[32px]"
					aria-expanded={isOpen}
					aria-label={`Toggle ${team.team_name} team details`}
				>
					<span class="truncate max-w-[12rem]">{team.team_name}</span>
					<span class="text-[11px] opacity-80">
						{running} running / {team.teammates.length}
					</span>
					<svg class="w-3 h-3 transition-transform" class:rotate-180={isOpen} fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>
			{/each}
		</div>

		<!-- Expanded details for any opened teams -->
		{#each teams as team (team.team_name)}
			{#if expanded[team.team_name]}
				<div class="border-t border-border/60 px-2 sm:px-4 py-2 bg-background/60">
					<div class="text-[11px] uppercase tracking-wide text-muted-foreground mb-1.5 font-semibold">
						{team.team_name}
					</div>
					<ul class="space-y-1.5">
						{#each team.teammates as mate (mate.teammate_name)}
							{@const tokens = formatTokens(mate.usage?.total_tokens)}
							{@const key = teammateKey(team.team_name, mate.teammate_name)}
							<li class="flex items-center gap-2 text-xs min-h-[36px]">
								<span class="px-1.5 py-0.5 rounded-full {statusPillClass(mate.status)} flex-shrink-0">
									{mate.status}
								</span>
								<span class="font-medium text-foreground truncate">{mate.teammate_name}</span>
								{#if tokens || mate.usage?.tool_uses}
									<span class="text-muted-foreground flex-shrink-0">
										{#if mate.usage?.tool_uses}{mate.usage.tool_uses} tools{/if}
										{#if tokens}{mate.usage?.tool_uses ? ' \u00b7 ' : ''}{tokens} tokens{/if}
									</span>
								{/if}
								<div class="ml-auto flex-shrink-0">
									{#if mate.status === 'running' && typeof mate.pid === 'number'}
										<button
											type="button"
											onclick={() => handleKill(team, mate)}
											disabled={killingKey === key}
											class="min-h-[32px] px-2 py-1 text-[11px] font-medium rounded border border-red-500/40 text-red-600 hover:bg-red-500/10 active:bg-red-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
										>
											{killingKey === key ? 'Killing\u2026' : 'Kill'}
										</button>
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				</div>
			{/if}
		{/each}
	</div>
{/if}

<style>
	.no-scrollbar::-webkit-scrollbar {
		display: none;
	}
	.no-scrollbar {
		scrollbar-width: none;
	}
</style>
