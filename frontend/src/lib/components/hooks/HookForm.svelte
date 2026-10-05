<script lang="ts">
	/**
	 * HookForm - Inline form for creating/editing hooks
	 *
	 * Renders as plain form fields (no card wrapper or action buttons).
	 * The parent editor provides the Save button.
	 * Exposes save() and getIsValid() for parent to call/check via bind:this.
	 */
	import { untrack } from 'svelte';
	import { Bell, Zap, Power } from 'lucide-svelte';
	import type { Hook, HookCreate, HookUpdate, HookEventType, HookType } from '$lib/api/client';
	import { HOOK_EVENT_TYPES } from '$lib/api/client';
	import { projects } from '$lib/stores/app-data.svelte';
	import type { Project } from '$lib/stores/types';

	interface Props {
		hook?: Hook | null;
		onSave: (data: HookCreate | HookUpdate, projectId: string | null) => void;
	}

	let { hook = null, onSave }: Props = $props();

	// Capture initial values (intentionally not reactive to prop changes)
	const initial = untrack(() => ({
		name: hook?.name || '',
		scopeProjectId: hook?.project_id ?? null,
		event: (hook?.event || 'Stop') as HookEventType,
		hookType: (hook?.hook_type || 'bash') as HookType,
		command: hook?.command || '',
		timeout: hook?.timeout || 30000,
		url: hook?.url || '',
		secret: hook?.secret || '',
		toolMatcher: hook?.tool_matcher || '',
		enabled: hook?.enabled ?? true,
	}));

	// Form state
	let name = $state(initial.name);
	let scopeProjectId = $state<string | null>(initial.scopeProjectId);
	let event = $state<HookEventType>(initial.event);
	let hookType = $state<HookType>(initial.hookType);
	let command = $state(initial.command);
	let timeout = $state(initial.timeout);
	let url = $state(initial.url);
	let secret = $state(initial.secret);
	let toolMatcher = $state(initial.toolMatcher);
	let enabled = $state(initial.enabled);

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
			enabled
		};

		onSave(data, scopeProjectId);
	}

	const eventDescriptions: Record<string, string> = {
		PreToolUse: 'Fires before a tool is invoked (can filter by tool name)',
		PostToolUse: 'Fires after a tool completes (can filter by tool name)',
		Stop: 'Fires when the agent finishes a response',
		SessionStart: 'Fires when a new session starts',
		SessionEnd: 'Fires when a session is closed',
		UserPromptSubmit: 'Fires when user submits a prompt',
		PreCompact: 'Fires before message history is compacted',
		SubagentStop: 'Fires when a subagent completes',
		Notification: 'Fires when the agent sends a notification'
	};
	let remoteChange = $state('');
	function snapshot() { return JSON.stringify([name, scopeProjectId, event, hookType, command, timeout, url, secret, toolMatcher, enabled]); }
	let baseline = snapshot();
	export function refreshRemote(fresh: Hook | undefined, saving = false): boolean {
		if (saving || snapshot() !== baseline) {
			remoteChange = fresh ? 'This hook changed elsewhere. Your unsaved changes are kept here.' : 'This hook was deleted elsewhere. Copy your unsaved changes before leaving.';
			return false;
		}
		if (!fresh) return true;
		name = fresh.name;
		scopeProjectId = fresh.project_id ?? null;
		event = fresh.event;
		hookType = fresh.hook_type;
		command = fresh.command || '';
		timeout = fresh.timeout || 30000;
		url = fresh.url || '';
		secret = fresh.secret || '';
		toolMatcher = fresh.tool_matcher || '';
		enabled = fresh.enabled;
		baseline = snapshot();
		remoteChange = '';
		return true;
	}
</script>

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}

<!-- Trigger -->
<div class="set-group">
	<div class="set-grouphead"><h3><Bell />Trigger</h3><p>When and where this hook fires.</p></div>
	<div class="set-row">
		<div class="lab"><h4>Name <span class="req">*</span></h4><p>A label to recognize this hook.</p></div>
		<div class="ctl"><input type="text" bind:value={name} placeholder="e.g., Slack Notification" class="inp" /></div>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Scope</h4><p>{scopeProjectId ? 'Fires only for sessions in this project.' : 'Fires for all sessions regardless of project.'}</p></div>
		<div class="ctl">
			<select bind:value={scopeProjectId} class="sel">
				<option value={null}>App (all sessions)</option>
				{#each $projects as project (project.id)}
					<option value={project.id}>{project.name}</option>
				{/each}
			</select>
		</div>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Event</h4><p>{eventDescriptions[event]}</p></div>
		<div class="ctl">
			<select bind:value={event} class="sel">
				{#each HOOK_EVENT_TYPES as eventType}
					<option value={eventType}>{eventType}</option>
				{/each}
			</select>
		</div>
	</div>
	{#if isToolEvent}
		<div class="set-row">
			<div class="lab"><h4>Tool matcher</h4><p>Regex to filter which tools trigger the hook. Optional.</p></div>
			<div class="ctl"><input type="text" bind:value={toolMatcher} placeholder="e.g., Bash|Edit|Write" class="inp mono" /></div>
		</div>
	{/if}
</div>

<!-- Action -->
<div class="set-group">
	<div class="set-grouphead"><h3><Zap />Action</h3><p>What this hook does when it runs.</p></div>
	<div class="set-row">
		<div class="lab"><h4>Type</h4><p>Run a command or call a webhook.</p></div>
		<div class="ctl">
			<div class="seg">
				<button type="button" class:on={hookType === 'bash'} onclick={() => hookType = 'bash'}>Bash</button>
				<button type="button" class:on={hookType === 'webhook'} onclick={() => hookType = 'webhook'}>Webhook</button>
			</div>
		</div>
	</div>

	{#if hookType === 'bash'}
		<div class="set-row stack">
			<div class="lab"><h4>Command <span class="req">*</span></h4><p>Shell command to execute. Event data is available as environment variables.</p></div>
			<div class="ctl">
				<textarea
					bind:value={command}
					placeholder={'e.g., curl -X POST https://slack.webhook.url -d \'{"text":"Session complete"}\''}
					class="fld code"
					rows="3"
				></textarea>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Timeout</h4><p>Maximum execution time in ms (1–300 seconds).</p></div>
			<div class="ctl"><input type="number" bind:value={timeout} min="1000" max="300000" step="1000" class="inp" /></div>
		</div>
	{:else if hookType === 'webhook'}
		<div class="set-row stack">
			<div class="lab"><h4>URL <span class="req">*</span></h4><p>HTTP endpoint to POST event data to.</p></div>
			<div class="ctl"><input type="url" bind:value={url} placeholder="https://example.com/webhook" class="inp mono" /></div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Secret</h4><p>Signs the payload via the X-Hook-Signature header. Optional.</p></div>
			<div class="ctl"><input type="password" bind:value={secret} placeholder="HMAC secret for signature" class="inp" /></div>
		</div>
	{/if}
</div>

<!-- Status -->
<div class="set-group">
	<div class="set-grouphead"><h3><Power />Status</h3></div>
	<div class="set-row">
		<div class="lab"><h4>Enabled</h4><p>Turn this hook on or off without deleting it.</p></div>
		<div class="ctl">
			<button
				type="button"
				class="switch"
				class:on={enabled}
				role="switch"
				aria-checked={enabled}
				aria-label="Toggle hook enabled"
				onclick={() => (enabled = !enabled)}
			><i></i></button>
		</div>
	</div>
</div>

<style>
	.req {
		color: var(--destructive);
		font-weight: 700;
	}
</style>
