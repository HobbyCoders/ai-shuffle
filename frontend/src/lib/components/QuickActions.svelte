<script lang="ts">
	/**
	 * QuickActions - Quick action buttons for common subagent tasks
	 *
	 * Provides a compact, responsive UI for quickly invoking specialized agents.
	 * Shows as a pill-style menu on desktop and a bottom sheet on mobile.
	 */
	import { api } from '$lib/api/client';

	interface Subagent {
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
	}

	interface Props {
		profileId: string;
		onAction: (prompt: string) => void;
		disabled?: boolean;
	}

	let { profileId, onAction, disabled = false }: Props = $props();

	let subagents = $state<Subagent[]>([]);
	let loading = $state(true);
	let showMenu = $state(false);

	// Quick action icons
	function getAgentIcon(name: string): string {
		switch (name) {
			case 'research-assistant':
				return 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z'; // Search
			case 'code-reviewer':
				return 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z'; // Check circle
			case 'test-generator':
				return 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4'; // Clipboard check
			case 'bug-investigator':
				return 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z'; // Exclamation triangle
			default:
				return 'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z'; // Users
		}
	}

	// Load subagents when profile changes
	$effect(() => {
		if (profileId) {
			loadSubagents();
		}
	});

	async function loadSubagents() {
		loading = true;
		try {
			subagents = await api.get<Subagent[]>(`/profiles/${profileId}/agents`);
		} catch (e) {
			subagents = [];
		} finally {
			loading = false;
		}
	}

	function handleAction(agent: Subagent) {
		// Create a prompt that triggers the subagent
		const prompt = `Use the ${agent.name} subagent to help me. ${agent.description}`;
		onAction(prompt);
		showMenu = false;
	}

	function toggleMenu() {
		showMenu = !showMenu;
	}

	function closeMenu() {
		showMenu = false;
	}
</script>

<!-- Main trigger button -->
{#if subagents.length > 0}
	<div class="relative">
		<button
			type="button"
			onclick={toggleMenu}
			class="flex-shrink-0 w-10 h-10 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg transition-colors disabled:opacity-50"
			{disabled}
			title="Quick actions"
		>
			<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
			</svg>
		</button>

		<!-- Desktop dropdown menu -->
		{#if showMenu}
			<!-- Backdrop for closing -->
			<button
				class="fixed inset-0 bg-transparent z-40"
				onclick={closeMenu}
				aria-label="Close menu"
			></button>

			<!-- Desktop menu (hidden on mobile) -->
			<div class="hidden sm:block absolute bottom-full left-0 mb-2 w-64 bg-card border border-border rounded-lg shadow-lg z-50 overflow-hidden">
				<div class="px-3 py-2 border-b border-border bg-muted/30">
					<span class="text-xs font-medium text-muted-foreground uppercase tracking-wider">Quick Actions</span>
				</div>
				<div class="max-h-64 overflow-y-auto">
					{#each subagents as agent (agent.name)}
						<button
							onclick={() => handleAction(agent)}
							class="w-full px-3 py-2 flex items-start gap-2 hover:bg-muted/50 transition-colors text-left"
						>
							<svg class="w-4 h-4 text-primary mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getAgentIcon(agent.name)} />
							</svg>
							<div class="flex-1 min-w-0">
								<div class="text-sm font-medium text-foreground">{agent.name}</div>
								<div class="text-xs text-muted-foreground line-clamp-1">{agent.description}</div>
							</div>
						</button>
					{/each}
				</div>
			</div>

			<!-- Mobile bottom sheet -->
			<div class="sm:hidden fixed inset-0 bg-black/50 z-50 flex items-end justify-center">
				<div class="w-full bg-card border-t border-border rounded-t-2xl shadow-lg max-h-[70vh] overflow-hidden">
					<div class="flex items-center justify-between px-4 py-3 border-b border-border">
						<span class="text-sm font-medium text-foreground">Quick Actions</span>
						<button onclick={closeMenu} class="p-1 hover:bg-muted rounded-md">
							<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						</button>
					</div>
					<div class="overflow-y-auto max-h-[60vh]">
						{#each subagents as agent (agent.name)}
							<button
								onclick={() => handleAction(agent)}
								class="w-full px-4 py-3 flex items-start gap-3 hover:bg-muted/50 transition-colors text-left border-b border-border/50 last:border-b-0"
							>
								<div class="w-10 h-10 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0">
									<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getAgentIcon(agent.name)} />
									</svg>
								</div>
								<div class="flex-1 min-w-0">
									<div class="font-medium text-foreground">{agent.name}</div>
									<div class="text-sm text-muted-foreground line-clamp-2">{agent.description}</div>
								</div>
							</button>
						{/each}
					</div>
				</div>
			</div>
		{/if}
	</div>
{/if}
