<script lang="ts">
	/**
	 * TeamStatusBar - Compact horizontal bar summarizing active agent teams
	 * for the current session.
	 *
	 * Team state is driven by the SESSION FEED, not by a timer: the SSE feed's
	 * `team.member_*` events are already reduced into `team_member` transcript
	 * cards, so the transcript says whether this session has teams and whether
	 * any teammate is running. The REST endpoint is only asked for what those
	 * cards do not carry (pid, token usage), and only while there is something
	 * to ask about — see team-status-poll.ts and PERFORMANCE_REVIEW §3.6 F4,
	 * which measured the old unconditional 2 s poll (per open session, auth + DB
	 * access check per hit, no visibility gate).
	 */
	import { listTeams, killTeammate, type Team, type Teammate } from '$lib/api/client';
	import { activeSession } from '$lib/stores/active-session';
	import {
		deriveTeamFeedSignal,
		isTeamAccessRefused,
		resolveTeamPollIntervalMs
	} from './team-status-poll';

	interface Props {
		sessionId: string;
	}

	let { sessionId }: Props = $props();

	let teams = $state<Team[]>([]);
	let expanded = $state<Record<string, boolean>>({});
	let killingKey = $state<string | null>(null);
	/** The session whose teams the server refused (403/404); asked no more. */
	let refusedFor = $state<string | null>(null);
	const refused = $derived(refusedFor !== null && refusedFor === sessionId);
	let documentVisible = $state(
		typeof document === 'undefined' || document.visibilityState !== 'hidden'
	);

	/** Team presence/activity as reported by the feed's own team cards. */
	const feedSignal = $derived(deriveTeamFeedSignal($activeSession.messages));
	// Read through PRIMITIVE deriveds: the signal object is rebuilt on every
	// streaming fold, so effects must depend on its values (string/boolean,
	// short-circuited by Svelte on `===`) and not on the object identity — the
	// difference between one fetch per team change and one fetch per frame.
	const teamSignature = $derived(feedSignal.signature);

	/** A team is "live" if either the feed or the last poll says a mate is running. */
	const hasRunning = $derived(
		feedSignal.hasRunning || teams.some((team) => runningCount(team) > 0)
	);
	const hasTeams: boolean = $derived(feedSignal.hasTeams || teams.length > 0);

	const pollIntervalMs = $derived(
		resolveTeamPollIntervalMs({ visible: documentVisible, hasTeams, hasRunning, refused })
	);

	function teammateKey(team: string, member: string): string {
		return `${team}::${member}`;
	}

	async function refresh(id: string) {
		try {
			const next = await listTeams(id);
			teams = Array.isArray(next) ? next : [];
		} catch (error) {
			// Quiet either way. A refusal will not change by asking again, so
			// this session is not polled again; anything else retries.
			if (isTeamAccessRefused(error)) {
				refusedFor = id;
				teams = [];
			}
		}
	}

	$effect(() => {
		if (typeof document === 'undefined') return;
		const onVisibility = () => {
			documentVisible = document.visibilityState !== 'hidden';
		};
		document.addEventListener('visibilitychange', onVisibility);
		return () => document.removeEventListener('visibilitychange', onVisibility);
	});

	// One fetch per session, plus one whenever the feed reports a team change
	// (a team appearing, a teammate finishing). Sessions that never run a team
	// therefore issue exactly one request — and none at all once the feed has
	// spoken, since the signature only changes when team cards do.
	$effect(() => {
		const id = sessionId;
		if (!id) {
			teams = [];
			return;
		}
		// Tracked so a feed-side change re-fetches the details immediately.
		void teamSignature;
		if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
		if (refusedFor === id) return;
		void refresh(id);
	});

	// Cadence while a team is live: fast enough to watch teammates finish, and
	// nothing at all when there are no teams or the tab is hidden.
	$effect(() => {
		const id = sessionId;
		const interval = pollIntervalMs;
		if (!id || interval === null) return;
		const handle = setInterval(() => void refresh(id), interval);
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
	<div class="sticky top-0 z-10 bg-muted/30 border-b border-border backdrop-blur-xs">
		<div class="flex items-center gap-2 overflow-x-auto px-2 sm:px-4 py-1.5 sm:py-2 no-scrollbar">
			<!-- Teams icon -->
			<svg class="w-4 h-4 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
			</svg>

			{#each teams as team (team.team_name)}
				{@const running = runningCount(team)}
				{@const isOpen = expanded[team.team_name] === true}
				<button
					type="button"
					onclick={() => toggle(team.team_name)}
					class="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-primary/10 text-primary border border-primary/20 hover:bg-primary/20 transition-colors shrink-0 min-h-[32px]"
					aria-expanded={isOpen}
					aria-label={`Toggle ${team.team_name} team details`}
				>
					<span class="truncate max-w-48">{team.team_name}</span>
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
								<span class="px-1.5 py-0.5 rounded-full {statusPillClass(mate.status)} shrink-0">
									{mate.status}
								</span>
								<span class="font-medium text-foreground truncate">{mate.teammate_name}</span>
								{#if tokens || mate.usage?.tool_uses}
									<span class="text-muted-foreground shrink-0">
										{#if mate.usage?.tool_uses}{mate.usage.tool_uses} tools{/if}
										{#if tokens}{mate.usage?.tool_uses ? ' \u00b7 ' : ''}{tokens} tokens{/if}
									</span>
								{/if}
								<div class="ml-auto shrink-0">
									{#if mate.status === 'running' && typeof mate.pid === 'number'}
										<button
											type="button"
											onclick={() => handleKill(team, mate)}
											disabled={killingKey === key}
											class="min-h-[32px] px-2 py-1 text-[11px] font-medium rounded-sm border border-red-500/40 text-red-600 hover:bg-red-500/10 active:bg-red-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
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
