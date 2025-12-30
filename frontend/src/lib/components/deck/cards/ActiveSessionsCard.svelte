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
		return project?.name || projectId;
	}

	function formatLastActivity(isoString: string | null): string {
		if (!isoString) return 'Unknown';
		const date = new Date(isoString);
		const now = new Date();
		const diffMs = now.getTime() - date.getTime();
		const diffSec = Math.floor(diffMs / 1000);
		const diffMin = Math.floor(diffSec / 60);
		const diffHour = Math.floor(diffMin / 60);

		if (diffSec < 60) return 'Just now';
		if (diffMin < 60) return `${diffMin}m ago`;
		if (diffHour < 24) return `${diffHour}h ago`;
		return date.toLocaleDateString();
	}
</script>

<BaseCard
	{card}
	title="Active Sessions"
	{onClose}
	{onMaximize}
	{onFocus}
	{onMove}
	{onResize}
	{onDragEnd}
	{onResizeEnd}
	{mobile}
>
	<div class="active-sessions-content">
		{#if loading}
			<div class="loading-state">
				<div class="spinner"></div>
				<span>Loading active sessions...</span>
			</div>
		{:else if error}
			<div class="error-state">
				<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path
						stroke-linecap="round"
						stroke-linejoin="round"
						stroke-width="2"
						d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
					/>
				</svg>
				<span>{error}</span>
				<button class="retry-btn" onclick={loadActiveSessions}>Retry</button>
			</div>
		{:else if activeSessions.length === 0}
			<div class="empty-state">
				<svg class="w-12 h-12 opacity-40" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path
						stroke-linecap="round"
						stroke-linejoin="round"
						stroke-width="1.5"
						d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
					/>
				</svg>
				<h3>No Active Sessions</h3>
				<p>There are no sessions with running Claude CLI processes.</p>
			</div>
		{:else}
			<div class="sessions-list">
				{#each activeSessions as session (session.session_id)}
					<div class="session-item" class:streaming={session.is_streaming}>
						<div class="session-info">
							<div class="session-header">
								<span class="session-title">{getSessionTitle(session)}</span>
								<div class="session-badges">
									{#if session.is_streaming}
										<span class="badge streaming">
											<span class="pulse"></span>
											Streaming
										</span>
									{:else if session.is_connected}
										<span class="badge connected">Connected</span>
									{/if}
									{#if session.worktree_id}
										<span class="badge worktree">Worktree</span>
									{/if}
								</div>
							</div>
							<div class="session-meta">
								{#if session.project_id}
									<span class="meta-item">
										<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												stroke-width="2"
												d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"
											/>
										</svg>
										{getProjectName(session.project_id)}
									</span>
								{/if}
								<span class="meta-item">
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
										/>
									</svg>
									{formatLastActivity(session.last_activity)}
								</span>
							</div>
						</div>
						<button
							class="close-btn"
							onclick={() => closeSession(session.session_id)}
							disabled={closingSession === session.session_id}
							title="Close session to release file handles"
						>
							{#if closingSession === session.session_id}
								<div class="spinner small"></div>
							{:else}
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										stroke-width="2"
										d="M6 18L18 6M6 6l12 12"
									/>
								</svg>
							{/if}
						</button>
					</div>
				{/each}
			</div>
		{/if}

		<div class="info-footer">
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path
					stroke-linecap="round"
					stroke-linejoin="round"
					stroke-width="2"
					d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
				/>
			</svg>
			<span
				>Active sessions hold file handles. Close sessions before deleting worktrees to avoid
				locked folders.</span
			>
		</div>
	</div>
</BaseCard>

<style>
	.active-sessions-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		padding: 1rem;
		gap: 1rem;
	}

	.loading-state,
	.error-state,
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.75rem;
		padding: 2rem;
		text-align: center;
		color: var(--muted-foreground);
		flex: 1;
	}

	.error-state {
		color: var(--destructive);
	}

	.empty-state h3 {
		font-size: 1rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.empty-state p {
		font-size: 0.875rem;
		margin: 0;
	}

	.spinner {
		width: 24px;
		height: 24px;
		border: 2px solid var(--border);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	.spinner.small {
		width: 16px;
		height: 16px;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.retry-btn {
		padding: 0.5rem 1rem;
		font-size: 0.875rem;
		background: var(--primary);
		color: var(--primary-foreground);
		border-radius: 0.375rem;
		transition: opacity 0.15s;
	}

	.retry-btn:hover {
		opacity: 0.9;
	}

	.sessions-list {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		flex: 1;
		overflow-y: auto;
	}

	.session-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.75rem;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		gap: 0.75rem;
		transition: border-color 0.15s;
	}

	.session-item:hover {
		border-color: var(--primary);
	}

	.session-item.streaming {
		border-color: var(--primary);
		background: color-mix(in srgb, var(--primary) 5%, var(--card));
	}

	.session-info {
		flex: 1;
		min-width: 0;
	}

	.session-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.session-title {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.session-badges {
		display: flex;
		gap: 0.25rem;
	}

	.badge {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.125rem 0.375rem;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		border-radius: 0.25rem;
	}

	.badge.streaming {
		background: color-mix(in srgb, var(--primary) 20%, transparent);
		color: var(--primary);
	}

	.badge.connected {
		background: color-mix(in srgb, var(--success, #22c55e) 20%, transparent);
		color: var(--success, #22c55e);
	}

	.badge.worktree {
		background: color-mix(in srgb, var(--warning, #f59e0b) 20%, transparent);
		color: var(--warning, #f59e0b);
	}

	.pulse {
		width: 6px;
		height: 6px;
		background: var(--primary);
		border-radius: 50%;
		animation: pulse 1.5s ease-in-out infinite;
	}

	@keyframes pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.4;
		}
	}

	.session-meta {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		margin-top: 0.25rem;
	}

	.meta-item {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: 0.375rem;
		color: var(--muted-foreground);
		transition: all 0.15s;
		flex-shrink: 0;
	}

	.close-btn:hover:not(:disabled) {
		background: color-mix(in srgb, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	.close-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.info-footer {
		display: flex;
		align-items: flex-start;
		gap: 0.5rem;
		padding: 0.75rem;
		background: color-mix(in srgb, var(--muted) 50%, transparent);
		border-radius: 0.5rem;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	.info-footer svg {
		flex-shrink: 0;
		margin-top: 0.125rem;
	}
</style>
