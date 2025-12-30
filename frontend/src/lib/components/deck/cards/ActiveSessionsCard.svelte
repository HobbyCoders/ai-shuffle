<script lang="ts">
	/**
	 * ActiveSessionsCard - Manage running SDK client sessions
	 *
	 * Shows all active sessions with connected Claude CLI processes.
	 * Allows users to close sessions to release file handles on working directories,
	 * which is necessary before deleting worktrees.
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth';
	import { sessions } from '$lib/stores/chat';
	import { worktrees } from '$lib/stores/git';
	import { projects } from '$lib/stores/tabs';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	interface ActiveSession {
		session_id: string;
		is_connected: boolean;
		is_streaming: boolean;
		last_activity: string | null;
		sdk_session_id: string | null;
		title: string | null;
		project_id: string | null;
		worktree_id: string | null;
	}

	let activeSessions: ActiveSession[] = $state([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let closingSession = $state<string | null>(null);
	let refreshInterval: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		loadActiveSessions();
		// Auto-refresh every 5 seconds
		refreshInterval = setInterval(loadActiveSessions, 5000);

		return () => {
			if (refreshInterval) {
				clearInterval(refreshInterval);
			}
		};
	});

	async function loadActiveSessions() {
		try {
			const response = await fetch('/api/v1/sessions/active', {
				headers: {
					Authorization: `Bearer ${$auth.token}`
				}
			});

			if (!response.ok) {
				throw new Error('Failed to load active sessions');
			}

			activeSessions = await response.json();
			error = null;
		} catch (e: any) {
			console.error('Failed to load active sessions:', e);
			error = e.message || 'Failed to load active sessions';
		} finally {
			loading = false;
		}
	}

	async function closeSession(sessionId: string) {
		closingSession = sessionId;
		try {
			const response = await fetch(`/api/v1/sessions/active/${sessionId}/close`, {
				method: 'POST',
				headers: {
					Authorization: `Bearer ${$auth.token}`
				}
			});

			if (!response.ok) {
				const data = await response.json();
				throw new Error(data.detail || 'Failed to close session');
			}

			// Remove from list
			activeSessions = activeSessions.filter((s) => s.session_id !== sessionId);
		} catch (e: any) {
			console.error('Failed to close session:', e);
			error = e.message || 'Failed to close session';
		} finally {
			closingSession = null;
		}
	}

	function getSessionTitle(session: ActiveSession): string {
		if (session.title) return session.title;
		return 'Untitled Session';
	}

	function getProjectName(projectId: string | null): string | null {
		if (!projectId) return null;
		const project = $projects.find((p) => p.id === projectId);
		return project?.name || null;
	}

	function getWorktreeBranch(worktreeId: string | null): string | null {
		if (!worktreeId) return null;
		const worktree = $worktrees.find((w) => w.id === worktreeId);
		return worktree?.branch_name || null;
	}

	function formatLastActivity(lastActivity: string | null): string {
		if (!lastActivity) return 'Unknown';
		const date = new Date(lastActivity);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffSecs = Math.floor(diffMs / 1000);
		const diffMins = Math.floor(diffSecs / 60);
		const diffHours = Math.floor(diffMins / 60);

		if (diffSecs < 60) return 'Just now';
		if (diffMins < 60) return `${diffMins}m ago`;
		if (diffHours < 24) return `${diffHours}h ago`;
		return date.toLocaleDateString();
	}
</script>

<BaseCard
	{card}
	{onClose}
	{onMaximize}
	{onFocus}
	{onMove}
	{onResize}
	{onDragEnd}
	{onResizeEnd}
>
	<div class="h-full flex flex-col bg-background">
		<!-- Header -->
		<div class="p-4 border-b border-border">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-lg font-semibold text-foreground">Active Sessions</h2>
					<p class="text-sm text-muted-foreground">
						Manage running Claude CLI processes
					</p>
				</div>
				<button
					onclick={() => loadActiveSessions()}
					class="p-2 rounded-lg hover:bg-muted transition-colors"
					title="Refresh"
				>
					<svg
						class="w-5 h-5 text-muted-foreground"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
						/>
					</svg>
				</button>
			</div>
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-auto p-4">
			{#if loading}
				<div class="flex items-center justify-center h-32">
					<div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
				</div>
			{:else if error}
				<div class="p-4 bg-destructive/10 border border-destructive/30 rounded-lg text-destructive">
					{error}
				</div>
			{:else if activeSessions.length === 0}
				<div class="text-center py-12 text-muted-foreground">
					<svg
						class="w-16 h-16 mx-auto mb-4 opacity-40"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="1.5"
							d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
						/>
					</svg>
					<p class="text-lg font-medium text-foreground mb-1">No Active Sessions</p>
					<p class="text-sm">All sessions are idle. No processes are running.</p>
				</div>
			{:else}
				<div class="space-y-3">
					{#each activeSessions as session (session.session_id)}
						{@const projectName = getProjectName(session.project_id)}
						{@const worktreeBranch = getWorktreeBranch(session.worktree_id)}
						<div
							class="p-4 bg-card border border-border rounded-lg hover:border-primary/30 transition-colors"
						>
							<div class="flex items-start justify-between gap-4">
								<div class="flex-1 min-w-0">
									<!-- Title and status -->
									<div class="flex items-center gap-2 flex-wrap">
										<span class="font-medium text-foreground truncate">
											{getSessionTitle(session)}
										</span>
										{#if session.is_streaming}
											<span
												class="px-1.5 py-0.5 text-[10px] uppercase rounded bg-green-500/15 text-green-500"
											>
												Streaming
											</span>
										{:else if session.is_connected}
											<span
												class="px-1.5 py-0.5 text-[10px] uppercase rounded bg-amber-500/15 text-amber-500"
											>
												Connected
											</span>
										{/if}
									</div>

									<!-- Project/Worktree info -->
									<div class="flex items-center gap-3 mt-1.5 text-xs text-muted-foreground">
										{#if projectName}
											<div class="flex items-center gap-1">
												<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														stroke-width="2"
														d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"
													/>
												</svg>
												<span>{projectName}</span>
											</div>
										{/if}
										{#if worktreeBranch}
											<div class="flex items-center gap-1">
												<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														stroke-width="2"
														d="M6 3v12m0 0c0 2.5 2 3 3.5 3s3.5-.5 3.5-3m-7 0c0-2.5 2-3 3.5-3s3.5.5 3.5 3m0 0v-6m0 0c0-2.5-2-3-3.5-3S6 6.5 6 9"
													/>
												</svg>
												<span>{worktreeBranch}</span>
											</div>
										{/if}
									</div>

									<!-- Last activity -->
									<div class="text-xs text-muted-foreground mt-1">
										Last active: {formatLastActivity(session.last_activity)}
									</div>
								</div>

								<!-- Close button -->
								<button
									onclick={() => closeSession(session.session_id)}
									disabled={closingSession === session.session_id}
									class="px-3 py-1.5 text-sm bg-destructive/10 text-destructive rounded-lg hover:bg-destructive/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
								>
									{#if closingSession === session.session_id}
										<div class="animate-spin rounded-full h-3 w-3 border-b-2 border-current"></div>
										Closing...
									{:else}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												stroke-width="2"
												d="M6 18L18 6M6 6l12 12"
											/>
										</svg>
										Close
									{/if}
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- Footer info -->
		<div class="p-4 border-t border-border bg-muted/30">
			<div class="flex items-start gap-2 text-xs text-muted-foreground">
				<svg class="w-4 h-4 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path
						stroke-linecap="round"
						stroke-linejoin="round"
						stroke-width="2"
						d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
					/>
				</svg>
				<p>
					Active sessions hold file handles on their working directory. Close sessions before
					deleting worktrees to avoid lock errors.
				</p>
			</div>
		</div>
	</div>
</BaseCard>
