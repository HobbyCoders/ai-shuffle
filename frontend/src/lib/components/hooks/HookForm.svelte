<script lang="ts">
	/**
	 * HookForm - Form for creating/editing hooks
	 */
	import { X, Loader2 } from 'lucide-svelte';
	import type { Hook, HookCreate, HookUpdate, HookEventType, HookType } from '$lib/api/client';
	import { HOOK_EVENT_TYPES } from '$lib/api/client';

	interface Props {
		hook?: Hook | null;
		saving?: boolean;
		onSave: (data: HookCreate | HookUpdate) => void;
		onCancel: () => void;
	}

	let { hook = null, saving = false, onSave, onCancel }: Props = $props();

	// Form state
	let name = $state(hook?.name || '');
	let event = $state<HookEventType>(hook?.event || 'Stop');
	let hookType = $state<HookType>(hook?.hook_type || 'bash');
	let command = $state(hook?.command || '');
	let timeout = $state(hook?.timeout || 30000);
	let url = $state(hook?.url || '');
	let secret = $state(hook?.secret || '');
	let toolMatcher = $state(hook?.tool_matcher || '');
	let enabled = $state(hook?.enabled ?? true);

	// Validation
	const isToolEvent = $derived(event === 'PreToolUse' || event === 'PostToolUse');
	const isValid = $derived(() => {
		if (!name.trim()) return false;
		if (hookType === 'bash' && !command.trim()) return false;
		if (hookType === 'webhook' && !url.trim()) return false;
		return true;
	});

	function handleSubmit() {
		if (!isValid()) return;

		const data: HookCreate | HookUpdate = {
			name: name.trim(),
			event,
			hook_type: hookType,
			command: hookType === 'bash' ? command.trim() : undefined,
			timeout,
			url: hookType === 'webhook' ? url.trim() : undefined,
			secret: hookType === 'webhook' && secret.trim() ? secret.trim() : undefined,
			tool_matcher: isToolEvent && toolMatcher.trim() ? toolMatcher.trim() : undefined,
			enabled
		};

		onSave(data);
	}

	const eventDescriptions: Record<string, string> = {
		PreToolUse: 'Fires before a tool is invoked (can filter by tool name)',
		PostToolUse: 'Fires after a tool completes (can filter by tool name)',
		Stop: 'Fires when Claude finishes a response',
		SessionStart: 'Fires when a new session starts',
		SessionEnd: 'Fires when a session is closed',
		UserPromptSubmit: 'Fires when user submits a prompt',
		PreCompact: 'Fires before message history is compacted',
		SubagentStop: 'Fires when a subagent completes',
		Notification: 'Fires when Claude sends a notification'
	};
</script>

<div class="hook-form">
	<div class="form-header">
		<h3>{hook ? 'Edit Hook' : 'New Hook'}</h3>
		<button class="close-btn" onclick={onCancel}>
			<X size={18} />
		</button>
	</div>

	<form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
		<div class="form-group">
			<label for="name">Name</label>
			<input
				id="name"
				type="text"
				bind:value={name}
				placeholder="e.g., Slack Notification"
				class="form-input"
			/>
		</div>

		<div class="form-group">
			<label for="event">Event Type</label>
			<select id="event" bind:value={event} class="form-select">
				{#each HOOK_EVENT_TYPES as eventType}
					<option value={eventType}>{eventType}</option>
				{/each}
			</select>
			<p class="form-hint">{eventDescriptions[event]}</p>
		</div>

		{#if isToolEvent}
			<div class="form-group">
				<label for="toolMatcher">Tool Matcher (optional)</label>
				<input
					id="toolMatcher"
					type="text"
					bind:value={toolMatcher}
					placeholder="e.g., Bash|Edit|Write"
					class="form-input mono"
				/>
				<p class="form-hint">Regex pattern to filter which tools trigger the hook</p>
			</div>
		{/if}

		<div class="form-group">
			<label>Hook Type</label>
			<div class="hook-type-selector">
				<button
					type="button"
					class="type-btn"
					class:active={hookType === 'bash'}
					onclick={() => hookType = 'bash'}
				>
					Bash Command
				</button>
				<button
					type="button"
					class="type-btn"
					class:active={hookType === 'webhook'}
					onclick={() => hookType = 'webhook'}
				>
					Webhook URL
				</button>
			</div>
		</div>

		{#if hookType === 'bash'}
			<div class="form-group">
				<label for="command">Command</label>
				<textarea
					id="command"
					bind:value={command}
					placeholder={'e.g., curl -X POST https://slack.webhook.url -d \'{"text":"Session complete"}\''}
					class="form-textarea mono"
					rows="3"
				></textarea>
				<p class="form-hint">Shell command to execute. Event data is available as environment variables.</p>
			</div>

			<div class="form-group">
				<label for="timeout">Timeout (ms)</label>
				<input
					id="timeout"
					type="number"
					bind:value={timeout}
					min="1000"
					max="300000"
					step="1000"
					class="form-input"
				/>
				<p class="form-hint">Maximum execution time (1-300 seconds)</p>
			</div>
		{:else}
			<div class="form-group">
				<label for="url">Webhook URL</label>
				<input
					id="url"
					type="url"
					bind:value={url}
					placeholder="https://example.com/webhook"
					class="form-input"
				/>
				<p class="form-hint">HTTP endpoint to POST event data to</p>
			</div>

			<div class="form-group">
				<label for="secret">Secret (optional)</label>
				<input
					id="secret"
					type="password"
					bind:value={secret}
					placeholder="HMAC secret for signature"
					class="form-input"
				/>
				<p class="form-hint">Used to sign the payload (X-Hook-Signature header)</p>
			</div>
		{/if}

		<div class="form-group checkbox-group">
			<label class="checkbox-label">
				<input type="checkbox" bind:checked={enabled} />
				<span>Enable hook</span>
			</label>
		</div>

		<div class="form-actions">
			<button type="button" class="btn-secondary" onclick={onCancel}>
				Cancel
			</button>
			<button
				type="submit"
				class="btn-primary"
				disabled={!isValid() || saving}
			>
				{#if saving}
					<Loader2 size={16} class="animate-spin" />
				{/if}
				{hook ? 'Save Changes' : 'Create Hook'}
			</button>
		</div>
	</form>
</div>

<style>
	.hook-form {
		padding: 20px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		max-width: 500px;
		width: 100%;
	}

	.form-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 20px;
	}

	.form-header h3 {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.close-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	form {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.form-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.form-group label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--muted-foreground);
	}

	.form-input,
	.form-select,
	.form-textarea {
		padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.form-input.mono,
	.form-textarea.mono {
		font-family: var(--font-mono);
		font-size: 0.8125rem;
	}

	.form-input::placeholder,
	.form-textarea::placeholder {
		color: var(--muted-foreground);
	}

	.form-input:focus,
	.form-select:focus,
	.form-textarea:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.form-textarea {
		resize: vertical;
		min-height: 80px;
	}

	.form-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0;
	}

	.hook-type-selector {
		display: flex;
		gap: 8px;
	}

	.type-btn {
		flex: 1;
		padding: 8px 12px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: var(--secondary);
		border: 1px solid var(--border);
		color: var(--muted-foreground);
		font-size: 0.875rem;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.type-btn:hover {
		border-color: var(--ring);
	}

	.type-btn.active {
		background: var(--selection-bg-emphasis);
		border-color: var(--selection-ring);
		color: var(--selection-text);
		box-shadow: var(--selection-shadow);
	}

	.checkbox-group {
		flex-direction: row;
	}

	.checkbox-label {
		display: flex;
		align-items: center;
		gap: 8px;
		cursor: pointer;
		font-weight: normal !important;
		color: var(--foreground) !important;
	}

	.checkbox-label input[type="checkbox"] {
		width: 16px;
		height: 16px;
		accent-color: var(--primary);
	}

	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 8px;
	}

	.btn-secondary,
	.btn-primary {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 10px 16px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		transition: all 150ms ease;
		border: none;
	}

	.btn-secondary {
		background: var(--secondary);
		color: var(--foreground);
	}

	.btn-secondary:hover {
		background: var(--hover-overlay);
		border-color: var(--accent);
	}

	.btn-primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn-primary:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Spinner animation */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
