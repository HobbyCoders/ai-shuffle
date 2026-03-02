<script lang="ts">
	/**
	 * HookForm - Inline form for creating/editing hooks
	 *
	 * Renders as plain form fields (no card wrapper or action buttons).
	 * Parent provides the Save button via FeaturePageLayout footer.
	 * Exposes save() and getIsValid() for parent to call/check via bind:this.
	 */
	import type { Hook, HookCreate, HookUpdate, HookEventType, HookType } from '$lib/api/client';
	import { HOOK_EVENT_TYPES } from '$lib/api/client';
	import { projects } from '$lib/stores/app-data';
	import type { Project } from '$lib/stores/types';

	interface Props {
		hook?: Hook | null;
		onSave: (data: HookCreate | HookUpdate, projectId: string | null) => void;
	}

	let { hook = null, onSave }: Props = $props();

	// Form state
	let name = $state(hook?.name || '');
	let scopeProjectId = $state<string | null>(hook?.project_id ?? null);
	let event = $state<HookEventType>(hook?.event || 'Stop');
	let hookType = $state<HookType>(hook?.hook_type || 'bash');
	let command = $state(hook?.command || '');
	let timeout = $state(hook?.timeout || 30000);
	let url = $state(hook?.url || '');
	let secret = $state(hook?.secret || '');
	let toolMatcher = $state(hook?.tool_matcher || '');
	let messageTemplate = $state(hook?.message_template || '');
	let enabled = $state(hook?.enabled ?? true);

	// Validation
	const isToolEvent = $derived(event === 'PreToolUse' || event === 'PostToolUse');

	/**
	 * Whether the form is currently valid.
	 * Exposed to parent for disabling the Save button.
	 */
	export function getIsValid(): boolean {
		if (!name.trim()) return false;
		if (hookType === 'bash' && !command.trim()) return false;
		if (hookType === 'webhook' && !url.trim()) return false;
		if (hookType === 'message' && !messageTemplate.trim()) return false;
		return true;
	}

	/**
	 * Gather form data and call onSave.
	 * Called by parent's footer Save button.
	 */
	export function save(): void {
		if (!getIsValid()) return;

		const data: HookCreate | HookUpdate = {
			name: name.trim(),
			event,
			hook_type: hookType,
			command: hookType === 'bash' ? command.trim() : undefined,
			timeout,
			url: hookType === 'webhook' ? url.trim() : undefined,
			secret: hookType === 'webhook' && secret.trim() ? secret.trim() : undefined,
			tool_matcher: isToolEvent && toolMatcher.trim() ? toolMatcher.trim() : undefined,
			message_template: hookType === 'message' ? messageTemplate.trim() : undefined,
			enabled
		};

		onSave(data, scopeProjectId);
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

<div class="hook-form-fields">
	<div class="form-group">
		<label for="hook-name">Name</label>
		<input
			id="hook-name"
			type="text"
			bind:value={name}
			placeholder="e.g., Slack Notification"
			class="form-input"
		/>
	</div>

	<div class="form-group">
		<label for="hook-scope">Scope</label>
		<select id="hook-scope" bind:value={scopeProjectId} class="form-select">
			<option value={null}>App (all sessions)</option>
			{#each $projects as project (project.id)}
				<option value={project.id}>{project.name}</option>
			{/each}
		</select>
		<p class="form-hint">{scopeProjectId ? 'Fires only for sessions in this project' : 'Fires for all sessions regardless of project'}</p>
	</div>

	<div class="form-group">
		<label for="hook-event">Event Type</label>
		<select id="hook-event" bind:value={event} class="form-select">
			{#each HOOK_EVENT_TYPES as eventType}
				<option value={eventType}>{eventType}</option>
			{/each}
		</select>
		<p class="form-hint">{eventDescriptions[event]}</p>
	</div>

	{#if isToolEvent}
		<div class="form-group">
			<label for="hook-toolMatcher">Tool Matcher (optional)</label>
			<input
				id="hook-toolMatcher"
				type="text"
				bind:value={toolMatcher}
				placeholder="e.g., Bash|Edit|Write"
				class="form-input mono"
			/>
			<p class="form-hint">Regex pattern to filter which tools trigger the hook</p>
		</div>
	{/if}

	<div class="form-group">
		<span class="form-label">Hook Type</span>
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
			<button
				type="button"
				class="type-btn"
				class:active={hookType === 'message'}
				onclick={() => hookType = 'message'}
			>
				Send Message
			</button>
		</div>
	</div>

	{#if hookType === 'bash'}
		<div class="form-group">
			<label for="hook-command">Command</label>
			<textarea
				id="hook-command"
				bind:value={command}
				placeholder={'e.g., curl -X POST https://slack.webhook.url -d \'{"text":"Session complete"}\''}
				class="form-textarea mono"
				rows="3"
			></textarea>
			<p class="form-hint">Shell command to execute. Event data is available as environment variables.</p>
		</div>

		<div class="form-group">
			<label for="hook-timeout">Timeout (ms)</label>
			<input
				id="hook-timeout"
				type="number"
				bind:value={timeout}
				min="1000"
				max="300000"
				step="1000"
				class="form-input"
			/>
			<p class="form-hint">Maximum execution time (1-300 seconds)</p>
		</div>
	{:else if hookType === 'webhook'}
		<div class="form-group">
			<label for="hook-url">Webhook URL</label>
			<input
				id="hook-url"
				type="url"
				bind:value={url}
				placeholder="https://example.com/webhook"
				class="form-input"
			/>
			<p class="form-hint">HTTP endpoint to POST event data to</p>
		</div>

		<div class="form-group">
			<label for="hook-secret">Secret (optional)</label>
			<input
				id="hook-secret"
				type="password"
				bind:value={secret}
				placeholder="HMAC secret for signature"
				class="form-input"
			/>
			<p class="form-hint">Used to sign the payload (X-Hook-Signature header)</p>
		</div>
	{:else if hookType === 'message'}
		<div class="form-group">
			<label for="hook-message-template">Message Template</label>
			<textarea
				id="hook-message-template"
				bind:value={messageTemplate}
				placeholder="e.g., Summarize the work done in this session and list any remaining tasks."
				class="form-textarea"
				rows="4"
			></textarea>
			<p class="form-hint">
				Prompt sent as a new user message in the active chat when this hook fires.
				Available variables: <code>{'{event}'}</code>, <code>{'{tool_name}'}</code>, <code>{'{timestamp}'}</code>, <code>{'{session_id}'}</code>
			</p>
		</div>
	{/if}

	<div class="form-group checkbox-group">
		<label class="checkbox-label">
			<input type="checkbox" bind:checked={enabled} />
			<span>Enable hook</span>
		</label>
	</div>
</div>

<style>
	.hook-form-fields {
		display: flex;
		flex-direction: column;
		gap: 16px;
		max-width: 640px;
		margin: 0 auto;
		padding: 16px;
	}

	.form-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.form-group label,
	.form-label {
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

	.form-hint code {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		padding: 1px 4px;
		background: var(--muted);
		border-radius: calc(var(--radius, 0.75rem) * 0.25);
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
</style>
