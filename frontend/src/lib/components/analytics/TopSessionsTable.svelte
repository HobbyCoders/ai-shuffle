<script lang="ts">
	import type { TopSession } from '$lib/api/client';

	export let sessions: TopSession[] = [];
	export let loading = false;
	export let onSessionClick: (sessionId: string) => void = () => {};

	// Format currency
	function formatCost(cost: number): string {
		return '$' + cost.toFixed(4);
	}

	// Format number
	function formatNumber(n: number): string {
		if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
		if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
		return n.toString();
	}

	// Format date
	function formatDate(dateStr: string): string {
		const date = new Date(dateStr);
		return date.toLocaleDateString('en-US', {
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	// Truncate title
	function truncateTitle(title: string | null, maxLength: number = 40): string {
		if (!title) return 'Untitled Session';
		if (title.length <= maxLength) return title;
		return title.substring(0, maxLength) + '...';
	}
</script>

<div class="w-full">
	<div class="flex items-center justify-between mb-4">
		<h3 class="text-lg font-semibold text-foreground">Top Sessions by Cost</h3>
	</div>

	{#if loading}
		<div class="h-[200px] flex items-center justify-center">
			<div class="animate-spin w-6 h-6 border-2 border-primary border-t-transparent rounded-full"></div>
		</div>
	{:else if sessions.length === 0}
		<div class="h-[200px] flex items-center justify-center bg-card rounded-xl border border-border">
			<div class="text-center text-muted-foreground">
				<svg class="w-12 h-12 mx-auto mb-2 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
				</svg>
				<p>No sessions for this period</p>
			</div>
		</div>
	{:else}
		<div class="bg-card rounded-xl border border-border overflow-hidden">
			<div class="overflow-x-auto">
				<table class="w-full">
					<thead>
						<tr class="border-b border-border bg-muted/30">
							<th class="text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider px-4 py-3">
								Session
							</th>
							<th class="text-left text-xs font-semibold text-muted-foreground uppercase tracking-wider px-4 py-3">
								Date
							</th>
							<th class="text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider px-4 py-3">
								Tokens
							</th>
							<th class="text-right text-xs font-semibold text-muted-foreground uppercase tracking-wider px-4 py-3">
								Cost
							</th>
							<th class="text-center text-xs font-semibold text-muted-foreground uppercase tracking-wider px-4 py-3">
								Action
							</th>
						</tr>
					</thead>
					<tbody>
						{#each sessions as session, i}
							<tr class="border-b border-border/50 hover:bg-muted/20 transition-colors">
								<td class="px-4 py-3">
									<div class="flex items-center gap-2">
										<span class="w-6 h-6 rounded-full bg-primary/20 text-primary text-xs font-medium flex items-center justify-center">
											{i + 1}
										</span>
										<span class="text-sm text-foreground" title={session.title || 'Untitled Session'}>
											{truncateTitle(session.title)}
										</span>
									</div>
								</td>
								<td class="px-4 py-3 text-sm text-muted-foreground whitespace-nowrap">
									{formatDate(session.created_at)}
								</td>
								<td class="px-4 py-3 text-sm text-muted-foreground text-right whitespace-nowrap">
									<span class="text-primary">{formatNumber(session.total_tokens_in)}</span>
									<span class="text-muted-foreground/60"> / </span>
									<span>{formatNumber(session.total_tokens_out)}</span>
								</td>
								<td class="px-4 py-3 text-sm font-medium text-foreground text-right whitespace-nowrap">
									{formatCost(session.total_cost_usd)}
								</td>
								<td class="px-4 py-3 text-center">
									<button
										on:click={() => onSessionClick(session.id)}
										class="text-xs px-2.5 py-1 rounded bg-primary/10 text-primary hover:bg-primary/20 transition-colors"
									>
										Open
									</button>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	{/if}
</div>
