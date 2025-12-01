<script lang="ts">
	/**
	 * RewindModal - V2 Rewind using direct JSONL manipulation
	 *
	 * This replaces the old PTY-based TerminalModal for rewind operations.
	 * It provides a clean UI for selecting checkpoints and executing rewind
	 * without any terminal/PTY complexity.
	 */

	interface Checkpoint {
		uuid: string;
		index: number;
		message_preview: string;
		full_message: string;
		timestamp?: string;
		git_available: boolean;
		git_ref?: string | null;
	}

	interface CheckpointsResponse {
		success: boolean;
		session_id: string;
		sdk_session_id?: string;
		checkpoints: Checkpoint[];
		error?: string;
	}

	interface RewindResponse {
		success: boolean;
		message: string;
		chat_rewound: boolean;
		code_rewound: boolean;
		messages_removed: number;
		error?: string;
	}

	interface Props {
		sessionId: string;
		onClose: () => void;
		onRewindComplete: (success: boolean, messagesRemoved: number) => void;
	}

	let { sessionId, onClose, onRewindComplete }: Props = $props();

	let checkpoints = $state<Checkpoint[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let selectedCheckpoint = $state<Checkpoint | null>(null);
	let executing = $state(false);

	// Rewind options
	let restoreChat = $state(true);
	let restoreCode = $state(false);
	let includeResponse = $state(true);

	// Load checkpoints on mount
	$effect(() => {
		loadCheckpoints();
	});

	async function loadCheckpoints() {
		loading = true;
		error = null;

		try {
			const response = await fetch(`/api/v1/commands/rewind/checkpoints/${sessionId}`, {
				credentials: 'include'
			});

			if (!response.ok) {
				throw new Error(`Failed to load checkpoints: ${response.statusText}`);
			}

			const data: CheckpointsResponse = await response.json();

			if (!data.success) {
				error = data.error || 'Failed to load checkpoints';
				return;
			}

			checkpoints = data.checkpoints;

			// Select the second-to-last checkpoint by default (rewind one step back)
			if (checkpoints.length > 1) {
				selectedCheckpoint = checkpoints[checkpoints.length - 2];
			} else if (checkpoints.length === 1) {
				selectedCheckpoint = checkpoints[0];
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Unknown error';
		} finally {
			loading = false;
		}
	}

	async function executeRewind() {
		if (!selectedCheckpoint) {
			error = 'Please select a checkpoint';
			return;
		}

		executing = true;
		error = null;

		try {
			const response = await fetch(`/api/v1/commands/rewind/execute/${sessionId}`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json'
				},
				credentials: 'include',
				body: JSON.stringify({
					target_uuid: selectedCheckpoint.uuid,
					restore_chat: restoreChat,
					restore_code: restoreCode,
					include_response: includeResponse
				})
			});

			if (!response.ok) {
				throw new Error(`Rewind failed: ${response.statusText}`);
			}

			const data: RewindResponse = await response.json();

			if (data.success) {
				onRewindComplete(true, data.messages_removed);
				onClose();
			} else {
				error = data.error || data.message || 'Rewind failed';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Unknown error';
		} finally {
			executing = false;
		}
	}

	function formatTimestamp(timestamp?: string): string {
		if (!timestamp) return '';
		try {
			const date = new Date(timestamp);
			return date.toLocaleString();
		} catch {
			return timestamp;
		}
	}

	function truncateMessage(msg: string, maxLength: number = 80): string {
		if (msg.length <= maxLength) return msg;
		return msg.substring(0, maxLength) + '...';
	}
</script>

<!-- Modal backdrop -->
<div
	class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
	onclick={(e) => e.target === e.currentTarget && onClose()}
	onkeydown={(e) => e.key === 'Escape' && onClose()}
	role="dialog"
	aria-modal="true"
	tabindex="-1"
>
	<!-- Modal content -->
	<div class="bg-background border border-border rounded-lg shadow-xl w-full max-w-2xl max-h-[80vh] flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-4 py-3 border-b border-border">
			<div class="flex items-center gap-2">
				<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0019 16V8a1 1 0 00-1.6-.8l-5.333 4zM4.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0011 16V8a1 1 0 00-1.6-.8l-5.334 4z" />
				</svg>
				<h2 class="text-lg font-semibold">Rewind Conversation</h2>
			</div>
			<button
				onclick={onClose}
				class="p-1 hover:bg-muted rounded transition-colors"
				aria-label="Close"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Body -->
		<div class="flex-1 overflow-y-auto p-4">
			{#if loading}
				<div class="flex items-center justify-center py-8">
					<div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
					<span class="ml-3 text-muted-foreground">Loading checkpoints...</span>
				</div>
			{:else if error}
				<div class="bg-destructive/10 border border-destructive/20 rounded-lg p-4 text-destructive">
					<div class="flex items-center gap-2">
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						<span>{error}</span>
					</div>
				</div>
			{:else if checkpoints.length === 0}
				<div class="text-center py-8 text-muted-foreground">
					<p>No checkpoints available for this session.</p>
					<p class="text-sm mt-2">Start a conversation to create checkpoints.</p>
				</div>
			{:else}
				<!-- Checkpoint list -->
				<div class="space-y-4">
					<div>
						<h3 class="text-sm font-medium text-muted-foreground mb-2">Select a checkpoint to rewind to:</h3>
						<div class="space-y-2 max-h-64 overflow-y-auto border border-border rounded-lg p-2">
							{#each checkpoints as checkpoint, i (checkpoint.uuid)}
								<button
									onclick={() => selectedCheckpoint = checkpoint}
									class="w-full text-left p-3 rounded-lg border transition-colors {selectedCheckpoint?.uuid === checkpoint.uuid
										? 'border-primary bg-primary/10'
										: 'border-border hover:border-muted-foreground/50 hover:bg-muted/50'}"
								>
									<div class="flex items-start justify-between gap-2">
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2">
												<span class="text-xs font-mono text-muted-foreground">#{i + 1}</span>
												{#if i === checkpoints.length - 1}
													<span class="text-xs bg-primary/20 text-primary px-1.5 py-0.5 rounded">Current</span>
												{/if}
												{#if checkpoint.git_ref}
													<span class="text-xs bg-green-500/20 text-green-600 dark:text-green-400 px-1.5 py-0.5 rounded" title="Git snapshot available">Git</span>
												{/if}
											</div>
											<p class="text-sm mt-1 truncate">{truncateMessage(checkpoint.message_preview)}</p>
											{#if checkpoint.timestamp}
												<p class="text-xs text-muted-foreground mt-1">{formatTimestamp(checkpoint.timestamp)}</p>
											{/if}
										</div>
										{#if selectedCheckpoint?.uuid === checkpoint.uuid}
											<svg class="w-5 h-5 text-primary flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
												<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
											</svg>
										{/if}
									</div>
								</button>
							{/each}
						</div>
					</div>

					<!-- Options -->
					<div class="border border-border rounded-lg p-4 space-y-3">
						<h3 class="text-sm font-medium text-muted-foreground">Restore options:</h3>

						<label class="flex items-center gap-3 cursor-pointer">
							<input
								type="checkbox"
								bind:checked={restoreChat}
								class="w-4 h-4 rounded border-border"
							/>
							<div>
								<span class="text-sm font-medium">Rewind conversation</span>
								<p class="text-xs text-muted-foreground">Truncate chat history to selected checkpoint</p>
							</div>
						</label>

						<label class="flex items-center gap-3 cursor-pointer {!selectedCheckpoint?.git_ref ? 'opacity-50' : ''}">
							<input
								type="checkbox"
								bind:checked={restoreCode}
								disabled={!selectedCheckpoint?.git_ref}
								class="w-4 h-4 rounded border-border"
							/>
							<div>
								<span class="text-sm font-medium">Restore code changes</span>
								<p class="text-xs text-muted-foreground">
									{#if selectedCheckpoint?.git_ref}
										Revert files to checkpoint state (git snapshot available)
									{:else if !selectedCheckpoint?.git_available}
										Not available - project is not a git repository
									{:else}
										No git snapshot for this checkpoint
									{/if}
								</p>
							</div>
						</label>

						<label class="flex items-center gap-3 cursor-pointer {!restoreChat ? 'opacity-50' : ''}">
							<input
								type="checkbox"
								bind:checked={includeResponse}
								disabled={!restoreChat}
								class="w-4 h-4 rounded border-border"
							/>
							<div>
								<span class="text-sm font-medium">Keep Claude's response</span>
								<p class="text-xs text-muted-foreground">Include the response to the selected message</p>
							</div>
						</label>
					</div>

					<!-- Warning -->
					{#if selectedCheckpoint && restoreChat}
						{@const messagesToRemove = checkpoints.length - selectedCheckpoint.index - (includeResponse ? 1 : 0)}
						{#if messagesToRemove > 0}
							<div class="bg-amber-500/10 border border-amber-500/20 rounded-lg p-3 text-amber-600 dark:text-amber-400">
								<div class="flex items-start gap-2">
									<svg class="w-5 h-5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
									</svg>
									<div class="text-sm">
										<p class="font-medium">This will remove approximately {messagesToRemove} message{messagesToRemove > 1 ? 's' : ''} from the conversation.</p>
										<p class="mt-1 text-xs opacity-80">This action cannot be undone. A backup will be created.</p>
									</div>
								</div>
							</div>
						{/if}
					{/if}
				</div>
			{/if}
		</div>

		<!-- Footer -->
		<div class="flex items-center justify-end gap-3 px-4 py-3 border-t border-border">
			<button
				onclick={onClose}
				class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
				disabled={executing}
			>
				Cancel
			</button>
			<button
				onclick={executeRewind}
				disabled={!selectedCheckpoint || executing || loading || (!restoreChat && !restoreCode)}
				class="px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
			>
				{#if executing}
					<div class="animate-spin rounded-full h-4 w-4 border-b-2 border-current"></div>
					Rewinding...
				{:else}
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0019 16V8a1 1 0 00-1.6-.8l-5.333 4zM4.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0011 16V8a1 1 0 00-1.6-.8l-5.334 4z" />
					</svg>
					Rewind
				{/if}
			</button>
		</div>
	</div>
</div>
