<script lang="ts">
	import type { ChatMessage } from '$lib/stores/types';
	import { Marked } from 'marked';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { Webhook, ChevronDown } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { notify } from '$lib/stores/notifications';
	import { resumeSession } from '$lib/api/sessions';

	// Create a local marked instance to avoid mutating global config
	const localMarked = new Marked({ breaks: true, gfm: true });

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	// ─── Hook event (SDK 0.1.81 hook pipeline) ────────────────────────────
	// Detect the hook_event subtype before falling through to the generic
	// system-message renderer. Hook events render as a collapsed chrome row
	// (mirrors compact_boundary) so noisy PreToolUse hooks don't dominate
	// the chat.
	const isHookEvent = $derived(message.systemSubtype === 'hook_event');

	type HookEventData = {
		hook_event_name: string | null;
		matcher: string | null;
		decision: string | null;
		reason: string | null;
		system_message: string | null;
		tool_name: string | null;
		timestamp: string;
	};

	const hookEventData = $derived<HookEventData | null>(
		isHookEvent && message.systemData
			? {
					hook_event_name:
						((message.systemData as Record<string, unknown>).hook_event_name as string | null) ??
						((message.systemData as Record<string, unknown>).hookEventName as string | null) ??
						null,
					matcher: ((message.systemData as Record<string, unknown>).matcher as string | null) ?? null,
					decision: ((message.systemData as Record<string, unknown>).decision as string | null) ?? null,
					reason: ((message.systemData as Record<string, unknown>).reason as string | null) ?? null,
					system_message:
						((message.systemData as Record<string, unknown>).system_message as string | null) ?? null,
					tool_name:
						((message.systemData as Record<string, unknown>).tool_name as string | null) ?? null,
					timestamp:
						((message.systemData as Record<string, unknown>).timestamp as string) ??
						new Date().toISOString(),
				}
			: null
	);

	let hookExpanded = $state(false);

	function formatHookTime(iso: string): string {
		try {
			const d = new Date(iso);
			if (Number.isNaN(d.getTime())) return '';
			return d.toLocaleTimeString('en-US', {
				hour: 'numeric',
				minute: '2-digit',
				hour12: true,
			});
		} catch {
			return '';
		}
	}

	function getDecisionBadgeClasses(decision: string | null): string {
		switch (decision) {
			case 'allow':
				return 'bg-success/15 text-success border-success/30';
			case 'deny':
				return 'bg-destructive/15 text-destructive border-destructive/30';
			case 'defer':
				return 'bg-warning/15 text-warning border-warning/30';
			default:
				return 'bg-muted text-muted-foreground border-border';
		}
	}

	function toggleHookExpanded() {
		hookExpanded = !hookExpanded;
	}

	function onHookKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			toggleHookExpanded();
		}
	}

	const hasHookDetails = $derived(
		!!hookEventData &&
			(!!hookEventData.reason ||
				!!hookEventData.system_message ||
				!!hookEventData.tool_name ||
				(!!hookEventData.matcher && hookEventData.matcher.length > 32))
	);

	// Check if content should be rendered as markdown
	function isMarkdownContent(subtype: string | undefined): boolean {
		return ['local_command'].includes(subtype || '');
	}

	function renderMarkdown(content: string): string {
		return localMarked.parse(content, { breaks: true }) as string;
	}

	// Format a task-notification duration into a compact human string (m/s).
	function formatDurationMs(v: unknown): string {
		const ms = Number(v);
		if (!Number.isFinite(ms) || ms <= 0) return '';
		const totalSec = Math.round(ms / 1000);
		const m = Math.floor(totalSec / 60);
		const s = totalSec % 60;
		return m > 0 ? `${m}m ${s}s` : `${s}s`;
	}

	// Build the compact trailing usage line for a task-notification card, e.g.
	// "— 88,851 tokens · 57 tool uses · 5m 12s". Returns '' when no usage present.
	function formatTaskUsage(usage: Record<string, unknown> | undefined): string {
		if (!usage) return '';
		const parts: string[] = [];
		const numOrRaw = (v: unknown): string => {
			const n = Number(v);
			return Number.isFinite(n) ? n.toLocaleString('en-US') : String(v);
		};
		const tokens = usage.subagent_tokens;
		if (tokens !== undefined && tokens !== null && tokens !== '') {
			parts.push(`${numOrRaw(tokens)} tokens`);
		}
		const toolUses = usage.tool_uses;
		if (toolUses !== undefined && toolUses !== null && toolUses !== '') {
			parts.push(`${numOrRaw(toolUses)} tool uses`);
		}
		const durationMs = usage.duration_ms;
		if (durationMs !== undefined && durationMs !== null && durationMs !== '') {
			const human = formatDurationMs(durationMs);
			if (human) parts.push(human);
		}
		return parts.length ? `— ${parts.join(' · ')}` : '';
	}

	// Shared status → badge-color mapping for task-notification cards/chips
	// (own card and quiet-pump chip). Keeps 'stopped' → amber consistent.
	function taskStatusColor(status: string): string {
		return status === 'failed' ? 'red'
			: status === 'completed' ? 'green'
			: status === 'stopped' ? 'amber'
			: 'gray';
	}

	// Get formatted content based on subtype
	function getFormattedContent(): { title: string; content: string; badge?: string; badgeColor?: string; hidden?: boolean; compact?: boolean; collapsible?: boolean; rawOutput?: string; rawInput?: string; taskStatus?: string } {
		const subtype = message.systemSubtype || 'system';
		const data: Record<string, unknown> = message.systemData || {};

		switch (subtype) {
			case 'status': {
				const status = data.status as string | null;
				const permissionMode = data.permissionMode as string | undefined;

				if (status === 'compacting') {
					return {
						title: 'Auto Compacting',
						content: 'Context is being automatically compacted to free up space...',
						badge: 'compacting',
						badgeColor: 'amber'
					};
				}

				// Permission mode change (plan mode, delegate mode, etc.)
				if (permissionMode) {
					const modeLabels: Record<string, string> = {
						plan: 'Plan Mode',
						default: 'Default Mode',
						auto: 'Auto Mode',
						acceptEdits: 'Accept Edits',
						bypassPermissions: 'Bypass Permissions',
						delegate: 'Delegate Mode',
						dontAsk: 'Deny Unapproved'
					};
					const label = modeLabels[permissionMode] || permissionMode;
					return {
						title: label,
						content: `Switched to ${label.toLowerCase()}.`,
						badge: permissionMode,
						badgeColor: permissionMode === 'plan' ? 'purple' : 'blue'
					};
				}

				// status: null with no permissionMode = clearing status (e.g., compaction ended)
				// Hide these since they carry no useful information
				if (!status) {
					return {
						title: 'Status Cleared',
						content: 'Previous status has been cleared.',
						badge: 'cleared',
						badgeColor: 'green',
						hidden: true
					};
				}

				// Hide no-op SDK telemetry status values. The SDK emits
				// subtype=status with status="requesting" on every stream_request_start
				// (once per model API call). These carry no user-relevant info and
				// clutter the chat between tool messages. Also hides older persisted
				// rows from before the backend filter was added.
				const NOOP_STATUS_VALUES = new Set([
					'requesting',
					'completed',
					'idle'
				]);
				if (NOOP_STATUS_VALUES.has(status)) {
					return {
						title: 'Status',
						content: status,
						badge: status,
						badgeColor: 'blue',
						hidden: true
					};
				}

				return {
					title: 'Status',
					content: status,
					badge: status,
					badgeColor: 'blue'
				};
			}

			case 'compact_boundary': {
				const metadata = data.compact_metadata as Record<string, unknown> || {};
				const trigger = metadata.trigger as string || 'manual';
				const preTokens = metadata.pre_tokens as number;

				let content = trigger === 'auto'
					? 'Context was automatically compacted.'
					: 'Context was compacted.';

				if (preTokens) {
					// Format large numbers nicely
					const formatTokens = (n: number) => {
						if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
						if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
						return n.toLocaleString();
					};
					content += ` Previous context: ${formatTokens(preTokens)} tokens.`;
				}

				return {
					title: 'Context Compacted',
					content,
					badge: preTokens ? `${Math.round(preTokens / 1000)}k → compacted` : (trigger === 'auto' ? 'auto' : 'manual'),
					badgeColor: 'green'
				};
			}

			case 'microcompact_boundary': {
				const metadata = data.microcompactMetadata as Record<string, unknown> || {};
				const trigger = metadata.trigger as string || 'auto';
				const tokensSaved = metadata.tokensSaved as number;

				let content = 'Context was microcompacted.';
				if (tokensSaved) {
					const formatTokens = (n: number) => {
						if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
						return n.toLocaleString();
					};
					content += ` Saved ${formatTokens(tokensSaved)} tokens.`;
				}

				return {
					title: 'Microcompacted',
					content,
					badge: trigger,
					badgeColor: 'green'
				};
			}

			case 'context': {
				// Format /context output from our custom handler
				const contextData = data as Record<string, unknown>;
				const lines: string[] = [];

				if (contextData.profile) {
					lines.push(`Profile: ${contextData.profile}`);
				}
				if (contextData.project) {
					lines.push(`Project: ${contextData.project}`);
				}
				if (contextData.working_directory) {
					lines.push(`Working Directory: ${contextData.working_directory}`);
				}
				if (contextData.model) {
					lines.push(`Model: ${contextData.model}`);
				}
				if (contextData.tools_available) {
					lines.push(`Tools: ${contextData.tools_available === 'all' ? 'All available' : `${contextData.tools_available} tools`}`);
				}
				if (contextData.session_id) {
					lines.push(`Session ID: ${contextData.session_id}`);
				}
				if (contextData.sdk_session_id) {
					lines.push(`SDK Session ID: ${contextData.sdk_session_id}`);
				}
				if (contextData.system_prompt_preview) {
					lines.push(`\nSystem Prompt Preview:\n${contextData.system_prompt_preview}`);
				}

				return {
					title: 'Current Context',
					content: lines.length > 0 ? lines.join('\n') : 'No context information available',
					badge: 'info',
					badgeColor: 'purple'
				};
			}

			case 'local_command': {
				// Output from SDK slash commands like /context, /compact
				// The SDK returns markdown-formatted content
				const content = (data.content as string) || message.content || '';

				// Determine the command type from content
				let title = 'Command Output';
				let badge = 'output';
				let badgeColor = 'blue';

				if (content.includes('Context Usage') || content.includes('**Model:**')) {
					title = 'Context Usage';
					badge = '/context';
					badgeColor = 'purple';
				} else if (content.includes('Compacted') || content.toLowerCase().includes('compact')) {
					title = 'Compacted';
					badge = '/compact';
					badgeColor = 'green';
				}

				return {
					title,
					content: content || 'Command executed',
					badge,
					badgeColor
				};
			}

			case 'informational': {
				// General informational messages from the SDK
				const content = (data.content as string) || message.content || 'Info';
				const level = data.level as string || 'info';
				return {
					title: 'Info',
					content,
					badge: level,
					badgeColor: level === 'warning' ? 'amber' : 'blue'
				};
			}

			case 'hook_started': {
				// Hook launch events — hidden because hook_response follows with full info
				const hookName = (data.hook_name as string) || (data.hookName as string) || (data.name as string) || '';
				const hookType = (data.hook_type as string) || (data.hookType as string) || (data.type as string) || '';
				const parts = [hookName, hookType].filter(Boolean);
				return {
					title: 'Hook Started',
					content: parts.length > 0 ? parts.join(' \u00b7 ') : 'hook executing',
					badge: hookType || 'hook',
					badgeColor: 'gray',
					hidden: true,
					compact: true
				};
			}

			case 'hook_response': {
				// Hook completion — collapsible with raw input + output
				const exitCode = (data.exitCode as number) ?? (data.exit_code as number) ?? null;
				// Default to success if no explicit error indicators
				const hasError = exitCode !== null && exitCode !== 0;
				const explicitFail = (data.success as boolean) === false;
				const success = !hasError && !explicitFail;
				const errMsg = (data.error as string) || (data.stderr as string) || '';
				const outMsg = (data.output as string) || (data.stdout as string) || (data.message as string) || '';
				const hookNameResp = (data.hook_name as string) || (data.hookName as string) || (data.name as string) || '';
				const rawOutput = errMsg || outMsg;

				// Build input summary. Hook events can carry the command that ran,
				// the stdin payload, the matcher, and/or the event name. Surface
				// whichever fields the SDK provided so users can see what was sent
				// to the hook (mirrors the Input/Output layout of tool messages).
				const command = (data.command as string) || (data.hook_command as string) || '';
				const matcher = (data.matcher as string) || (data.hook_matcher as string) || '';
				const eventName = (data.hook_event_name as string) || (data.hookEventName as string) || (data.event as string) || '';
				const source = (data.source as string) || '';
				const hookInputRaw =
					(data.hook_input as unknown) ??
					(data.hookInput as unknown) ??
					(data.input as unknown) ??
					(data.stdin as unknown) ??
					null;

				const inputParts: string[] = [];
				if (command) inputParts.push(command);
				const trigger = [eventName, source || matcher].filter(Boolean).join(':');
				if (trigger && trigger !== hookNameResp) inputParts.push(`trigger: ${trigger}`);
				if (hookInputRaw !== null && hookInputRaw !== undefined) {
					try {
						const stdinStr =
							typeof hookInputRaw === 'string'
								? hookInputRaw
								: JSON.stringify(hookInputRaw, null, 2);
						if (stdinStr && stdinStr !== '{}' && stdinStr !== '""') {
							inputParts.push(stdinStr);
						}
					} catch {
						// Ignore stringify failures.
					}
				}

				// Fallback: if no recognized input field was present, surface any
				// remaining metadata fields so the Input box is not empty when the
				// SDK/CLI emits a non-standard schema. Excludes the fields already
				// rendered elsewhere on the card (title/badge/output).
				if (inputParts.length === 0) {
					const CONSUMED_KEYS = new Set([
						'output', 'stdout', 'stderr', 'error', 'message',
						'exitCode', 'exit_code', 'success',
						'hook_name', 'hookName', 'name',
						'command', 'hook_command',
						'matcher', 'hook_matcher',
						'hook_event_name', 'hookEventName', 'event', 'source',
						'hook_input', 'hookInput', 'input', 'stdin'
					]);
					const remaining: Record<string, unknown> = {};
					for (const [key, value] of Object.entries(data)) {
						if (CONSUMED_KEYS.has(key)) continue;
						if (value === null || value === undefined || value === '') continue;
						remaining[key] = value;
					}
					if (Object.keys(remaining).length > 0) {
						try {
							inputParts.push(JSON.stringify(remaining, null, 2));
						} catch {
							// Ignore stringify failures.
						}
					}
				}
				const rawInput = inputParts.join('\n\n');

				// Short summary for header — hook name + truncated first line
				const firstLine = rawOutput.split('\n')[0] || '';
				const shortSnippet = firstLine.length > 60 ? firstLine.slice(0, 60) + '\u2026' : firstLine;
				const headerParts = [hookNameResp, shortSnippet].filter(Boolean);
				return {
					title: 'Hook',
					content: headerParts.length > 0 ? headerParts.join(' \u00b7 ') : (success ? 'completed' : 'failed'),
					badge: success ? 'success' : 'failed',
					badgeColor: success ? 'green' : 'red',
					collapsible: true,
					rawInput: rawInput || undefined,
					rawOutput: rawOutput || undefined
				};
			}

			case 'turn_duration': {
				// Turn timing info - hide these as they're internal metrics
				const durationMs = (data.durationMs as number) || 0;
				const seconds = (durationMs / 1000).toFixed(1);
				return {
					title: 'Turn Duration',
					content: `Turn completed in ${seconds}s.`,
					badge: `${seconds}s`,
					badgeColor: 'gray',
					hidden: true,
					compact: true
				};
			}

			case 'task_started': {
				// A background task (Monitor shell or sub-agent) is now RUNNING.
				// Rendered as the running twin of the `task_updated` chip below —
				// same 'Task' header, same status badge — so a long-lived shell
				// (a dev server, a watcher) has a visible affordance for as long
				// as it runs, instead of surfacing only when it dies.
				const startedDescription =
					(data.description as string) ||
					(data.summary as string) ||
					message.content ||
					((data.task_id as string) || '').slice(0, 8);
				const startedStatus = (data.status as string) || 'running';
				let startedRaw: string | undefined;
				try {
					startedRaw = JSON.stringify(data, null, 2);
				} catch {
					startedRaw = undefined;
				}
				return {
					title: 'Task',
					content: startedDescription,
					badge: startedStatus,
					badgeColor: taskStatusColor(startedStatus),
					taskStatus: startedStatus,
					collapsible: true,
					rawOutput: startedRaw
				};
			}

			case 'task_progress': {
				// Task/subagent progress updates - hide as they're internal SDK metrics
				return {
					title: 'Task Progress',
					content: (data.description as string) || message.content || '',
					badge: 'task_progress',
					badgeColor: 'gray',
					hidden: true,
					compact: true
				};
			}

			case 'thinking_tokens': {
				// Live thinking-token estimates - routed to the thinking-block
				// counter during streaming (never a card). Defensive guard so
				// any persisted rows that slip through reload stay hidden.
				return {
					title: 'Thinking Tokens',
					content: `${(data.estimated_tokens as number) ?? ''}`,
					badge: 'thinking_tokens',
					badgeColor: 'gray',
					hidden: true,
					compact: true
				};
			}

			case 'task_updated':
			case 'task_notification': {
				// Quiet-pump step notification: an internal step of a still-RUNNING
				// background agent, surfaced only so the model can react. Render an
				// ultra-compact, collapsed-by-default chip — muted "background step"
				// with the status, the result tucked behind the collapse, and NO
				// usage line. Distinct from an own notification's full 'Task' card.
				if (subtype === 'task_notification' && data.kind === 'pump') {
					const pumpStatus = (data.status as string) || '';
					const pumpResult =
						typeof data.result === 'string' && data.result.trim()
							? data.result
							: undefined;
					return {
						title: 'background step',
						content: pumpStatus,
						badge: pumpStatus || subtype,
						badgeColor: taskStatusColor(pumpStatus),
						taskStatus: pumpStatus || subtype,
						collapsible: true,
						rawOutput: pumpResult
					};
				}

				// SDK task state patches (status changes, timing, usage).
				// Internal telemetry — keep collapsible so users can inspect
				// the raw patch without a huge JSON card dominating the chat.
				const patch = (data.patch as Record<string, unknown>) || {};
				const taskId = (data.task_id as string) || '';
				const status =
					(patch.status as string) ||
					(data.status as string) ||
					'';
				const description =
					(patch.description as string) ||
					(data.description as string) ||
					(data.summary as string) ||
					(data.subject as string) ||
					'';

				const statusColor = taskStatusColor(status);

				let rawOutput: string | undefined;
				const resultText =
					typeof data.result === 'string' && data.result.trim() ? data.result : '';
				if (resultText) {
					// task_notification card: show the sub-agent's result markdown as
					// the expanded pane, with a compact usage line appended.
					rawOutput = resultText;
					const usageLine = formatTaskUsage(data.usage as Record<string, unknown> | undefined);
					if (usageLine) rawOutput += `\n\n${usageLine}`;
				} else {
					try {
						rawOutput = JSON.stringify(data, null, 2);
					} catch {
						rawOutput = undefined;
					}
				}

				return {
					// Header name label matches a tool card: a stable 'Task' label,
					// with the live status carried by the chip (badge) and the task's
					// description as the truncated summary.
					title: 'Task',
					content: description || (taskId ? taskId.slice(0, 8) : ''),
					badge: status || subtype,
					badgeColor: statusColor,
					taskStatus: status || subtype,
					collapsible: true,
					rawOutput
				};
			}

			case 'stop_hook_summary': {
				// Stop hook completion summary
				const hookCount = data.hookCount as number || 0;
				const hasOutput = data.hasOutput as boolean;
				return {
					title: 'Hook Summary',
					content: `${hookCount} hook${hookCount !== 1 ? 's' : ''} executed.${hasOutput ? ' Output was produced.' : ''}`,
					badge: `${hookCount} hooks`,
					badgeColor: 'gray',
					hidden: true,
					compact: true
				};
			}

			case 'agent_launched': {
				// Background agent was launched successfully
				return {
					title: 'Background Agent Launched',
					content: message.content || 'Agent is now running in the background.',
					badge: 'background',
					badgeColor: 'green'
				};
			}

			case 'agent_error': {
				// Background agent launch failed
				return {
					title: 'Agent Launch Failed',
					content: message.content || 'Failed to launch background agent.',
					badge: 'error',
					badgeColor: 'amber'
				};
			}

			case 'api_error': {
				// API error messages from Claude SDK (e.g., max output tokens exceeded)
				// Content may be in message.content (from JSONL parser) or data.content
				// (from streaming path via live_session_store)
				const apiErrorContent = message.content || (data.content as string) || 'An API error occurred';
				return {
					title: 'API Error',
					content: apiErrorContent,
					badge: 'api',
					badgeColor: 'red'
				};
			}

			case 'provider_retry': {
				// The engine got an empty 200 back and is re-sending the same
				// request. One card counts the attempts up (the reducer
				// replaces it in place), so this row reads as live progress,
				// not a stack of ten warnings.
				const attempt = data.attempt ?? '?';
				const maxAttempts = data.max_attempts ?? '?';
				const retryModel = typeof data.model === 'string' ? data.model : '';
				if (data.exhausted) {
					// The cap was reached and the turn died. The card stops
					// counting and becomes the failure notice — plus the way
					// back, via the Retry button on the row.
					let spent = `Empty response from the provider ${maxAttempts} times in a row. Fix the issue, then retry from here.`;
					if (retryModel) spent += ` · ${retryModel}`;
					// The card only leaves the failure state two ways: a resume
					// that actually got an answer back, or the user typing
					// something else instead.
					if (data.recovered) spent += ' · recovered';
					else if (data.superseded) spent += ' · superseded';
					return {
						title: 'Retry failed',
						content: spent,
						badge: 'failed',
						badgeColor: 'red',
						compact: true
					};
				}
				if (data.settled) {
					// The turn reached its end without exhausting the cap, so the
					// retries worked. The row stops counting and reports what it
					// cost — a settled outcome, no spinner and no Retry button.
					let recovered = `Empty response from the provider — recovered after ${attempt} ${attempt === 1 ? 'retry' : 'retries'}`;
					if (retryModel) recovered += ` · ${retryModel}`;
					return {
						title: 'Retried',
						content: recovered,
						badge: 'retry',
						badgeColor: 'green',
						compact: true
					};
				}
				const base = `Empty response from the provider — retrying (attempt ${attempt} of ${maxAttempts})`;
				return {
					title: 'Retrying',
					content: retryModel ? `${base} · ${retryModel}` : base,
					badge: 'retry',
					badgeColor: 'amber',
					compact: true
				};
			}

			case 'plugin_warning': {
				// Plugin hook stderr output (security warnings, etc.)
				const metadataPlugin = message.metadata?.plugin;
				const pluginName: string =
					(typeof data.plugin === 'string' ? data.plugin : null) ||
					(typeof metadataPlugin === 'string' ? metadataPlugin : null) ||
					'Plugin';
				const warningContent: string =
					(typeof data.message === 'string' ? data.message : null) ||
					(typeof message.content === 'string' ? message.content : null) ||
					'Plugin warning';
				return {
					title: pluginName,
					content: warningContent,
					badge: 'warning',
					badgeColor: 'amber'
				};
			}

			case 'rewind_warning': {
				// Rewind partial success warning (chat rewound but file restore unavailable)
				return {
					title: 'Rewind Notice',
					content: message.content || 'Chat was rewound but file restoration was unavailable.',
					badge: 'partial',
					badgeColor: 'amber'
				};
			}

			case 'error':
			case 'exception':
			case 'validation_error':
			case 'permission_error':
			case 'timeout_error': {
				// SDK errors and exceptions
				const errorMessage = (data.message as string) || (data.error as string) || message.content || 'An error occurred';
				return {
					title: 'Error',
					content: errorMessage,
					badge: subtype.replace(/_/g, ' '),
					badgeColor: 'red'
				};
			}

			default: {
				// For unknown subtypes, try to extract a meaningful field before falling back to JSON
				const isError = subtype.includes('error') || subtype.includes('fail') || subtype.includes('exception');
				let defaultContent: string;
				if (typeof data === 'object' && data !== null) {
					// Try common meaningful fields first
					const meaningful = (data.content as string) || (data.message as string) || (data.description as string) || (data.error as string) || (data.output as string) || (data.text as string);
					if (meaningful) {
						defaultContent = String(meaningful);
					} else {
						// Fall back to JSON, but truncate
						const json = JSON.stringify(data, null, 2);
						defaultContent = json.length > 200 ? json.slice(0, 200) + '\u2026' : json;
					}
				} else {
					defaultContent = String(data);
				}
				return {
					title: subtype.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
					content: defaultContent,
					badge: subtype,
					badgeColor: isError ? 'red' : 'gray'
				};
			}
		}
	}

	const formatted = $derived(getFormattedContent());

	// Background-task state patches render their collapsed header with the
	// tool-card convention (three-way status icon + bot icon + status chip)
	// rather than the generic hook lightning-bolt row.
	const isTaskSubtype = $derived(
		message.systemSubtype === 'task_started' ||
			message.systemSubtype === 'task_updated' ||
			message.systemSubtype === 'task_notification'
	);

	// A retry row is work in flight, not a settled outcome: it takes the
	// spinner the running-task rows use instead of a check or a cross. Once
	// the attempts are spent the turn is over, so the spinner gives way to the
	// settled cross and the row offers a way back.
	const retryExhausted = $derived(
		message.systemSubtype === 'provider_retry' &&
			!!(message.systemData as Record<string, unknown> | undefined)?.exhausted
	);
	// A resume launched from this card is in flight. The provider can stay
	// silent for a minute or two, so the row keeps spinning and the button
	// says "Retrying…" instead of vanishing.
	const retryResuming = $derived(
		retryExhausted && !!(message.systemData as Record<string, unknown> | undefined)?.resuming
	);
	// A reply actually came back — the failure is history, so the row takes the
	// green check and the button is gone for good.
	const retryRecovered = $derived(
		retryExhausted && !!(message.systemData as Record<string, unknown> | undefined)?.recovered
	);
	// The user typed something else instead of retrying. The card retires
	// without claiming the new turn's answer as its own.
	const retrySuperseded = $derived(
		retryExhausted && !!(message.systemData as Record<string, unknown> | undefined)?.superseded
	);
	// The reducer stamps `settled` on any terminal turn event, so a card that
	// counted a few attempts and then got its answer stops spinning and takes
	// the green check the other finished compact rows use.
	const retrySettled = $derived(
		message.systemSubtype === 'provider_retry' &&
			!!(message.systemData as Record<string, unknown> | undefined)?.settled
	);
	const isProviderRetry = $derived(
		message.systemSubtype === 'provider_retry' && !retryExhausted && !retrySettled
	);
	// The way back stays on offer until the session genuinely recovers, or the
	// user moves on. A resume that fails again re-arms it.
	const canRetry = $derived(retryExhausted && !retryRecovered && !retrySuperseded);

	// Covers the gap between the click and turn.started arriving; from then on
	// the reducer's `resuming` drives the disabled state.
	let retrying = $state(false);
	const retryInFlight = $derived(retrying || retryResuming);
	$effect(() => {
		if (retryResuming) retrying = false;
	});

	async function handleRetry() {
		const sessionId = $activeSession.sessionId;
		if (!sessionId || retryInFlight) return;
		retrying = true;
		try {
			await resumeSession(sessionId);
			// Leave the button disabled: turn.started stamps `resuming` and the
			// row keeps showing the work until a reply lands.
		} catch (e) {
			retrying = false;
			notify.error(
				`Couldn't retry — ${e instanceof Error ? e.message : 'please try again'}.`
			);
		}
	}

	// Badge color classes
	function getBadgeClasses(color: string): string {
		switch (color) {
			case 'amber': return 'bg-warning/20 text-warning';
			case 'green': return 'bg-success/20 text-success';
			case 'blue': return 'bg-info/20 text-info';
			case 'purple': return 'bg-primary/20 text-primary';
			case 'red': return 'bg-destructive/20 text-destructive';
			default: return 'bg-muted text-muted-foreground';
		}
	}
</script>

{#if isHookEvent && hookEventData}
	<!--
		Hook event row — collapsed chrome by default. Mirrors the visual
		restraint of compact_boundary; never as loud as a full system card.
		Whole row is the tap target (>=44px) so mobile users can expand
		on touch.
	-->
	<div class="w-full hook-event-row sys-flat">
		<div
			class="hook-event-summary sys-row w-full flex items-center gap-2 min-w-0 cursor-pointer select-none"
			role="button"
			tabindex="0"
			aria-expanded={hookExpanded}
			aria-label={`Hook event ${hookEventData.hook_event_name ?? ''}${hookEventData.decision ? ' decision ' + hookEventData.decision : ''}`}
			onclick={toggleHookExpanded}
			onkeydown={onHookKeydown}
		>
			<!-- Hook icon -->
			<Webhook class="w-4 h-4 text-primary/80 shrink-0" strokeWidth={2} />

			<!-- Primary label -->
			<span class="text-sm font-medium text-foreground truncate min-w-0">
				{hookEventData.hook_event_name ?? 'hook'}
			</span>

			<!-- Matcher chip -->
			{#if hookEventData.matcher}
				<span class="hook-matcher-chip shrink-0 px-1.5 py-0.5 text-[10px] font-mono rounded-sm bg-muted/60 text-muted-foreground border border-border/50 truncate max-w-[140px]"
					title={hookEventData.matcher}
				>
					{hookEventData.matcher}
				</span>
			{/if}

			<!-- Decision badge -->
			{#if hookEventData.decision}
				<span class="shrink-0 px-1.5 py-0.5 text-[10px] uppercase tracking-wide font-semibold rounded-sm border {getDecisionBadgeClasses(hookEventData.decision)}">
					{hookEventData.decision}
				</span>
			{/if}

			<!-- Spacer pushes time + chevron to the right -->
			<div class="flex-1 min-w-0"></div>

			<!-- Time chip -->
			{#if hookEventData.timestamp}
				<span class="hidden sm:inline text-[10px] text-muted-foreground/70 shrink-0 font-mono tabular-nums">
					{formatHookTime(hookEventData.timestamp)}
				</span>
			{/if}

			<!-- Chevron (only when there's something to expand) -->
			{#if hasHookDetails}
				<ChevronDown
					class="w-3.5 h-3.5 text-muted-foreground/70 shrink-0 transition-transform duration-150 {hookExpanded ? 'rotate-180' : ''}"
				/>
			{/if}
		</div>

		{#if hookExpanded && hasHookDetails}
			<div class="px-3 pb-3 pt-1 border-t border-border/40 bg-muted/20 space-y-2">
				{#if hookEventData.tool_name}
					<div class="hook-detail-row flex items-baseline gap-2">
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70 w-20 shrink-0">Tool</span>
						<span class="text-xs font-mono text-foreground break-all">{hookEventData.tool_name}</span>
					</div>
				{/if}
				{#if hookEventData.matcher}
					<div class="hook-detail-row flex items-baseline gap-2">
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70 w-20 shrink-0">Matcher</span>
						<span class="text-xs font-mono text-foreground break-all">{hookEventData.matcher}</span>
					</div>
				{/if}
				{#if hookEventData.reason}
					<div class="hook-detail-row flex items-baseline gap-2">
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70 w-20 shrink-0">Reason</span>
						<span class="text-xs text-foreground whitespace-pre-wrap wrap-break-word">{hookEventData.reason}</span>
					</div>
				{/if}
				{#if hookEventData.system_message}
					<div class="hook-detail-row flex items-baseline gap-2">
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70 w-20 shrink-0">Message</span>
						<span class="text-xs text-muted-foreground whitespace-pre-wrap wrap-break-word">{hookEventData.system_message}</span>
					</div>
				{/if}
				<!-- Time visible on mobile only when expanded -->
				<div class="sm:hidden hook-detail-row flex items-baseline gap-2">
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70 w-20 shrink-0">Time</span>
					<span class="text-xs font-mono text-muted-foreground tabular-nums">{formatHookTime(hookEventData.timestamp)}</span>
				</div>
			</div>
		{/if}
	</div>
{:else if !formatted.hidden}
	{#if formatted.collapsible}
		<!-- Collapsible rendering for hook responses — styled like tool messages -->
		<div class="w-full sys-flat">
			<details class="group/hook">
				<summary class="hook-summary sys-row w-full flex items-center gap-2 min-w-0 cursor-pointer list-none">
					{#if isTaskSubtype}
						<!-- Status icon — three-way, matching ToolUseMessage's convention -->
						{#if formatted.taskStatus === 'running' || formatted.taskStatus === 'pending'}
							<svg class="w-4 h-4 text-primary animate-spin shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
							</svg>
						{:else if formatted.taskStatus === 'failed' || formatted.taskStatus === 'error' || formatted.taskStatus === 'killed'}
							<svg class="w-4 h-4 text-red-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						{:else}
							<svg class="w-4 h-4 text-green-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
						{/if}
						<!-- Task icon (bot) — matches ToolUseMessage's Task entry -->
						<svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
							<rect x="3" y="11" width="18" height="10" rx="2"/>
							<circle cx="12" cy="5" r="2"/>
							<path d="M12 7v4"/>
							<line x1="8" y1="16" x2="8" y2="16"/>
							<line x1="16" y1="16" x2="16" y2="16"/>
						</svg>
					{:else}
						<!-- Status icon -->
						{#if formatted.badgeColor === 'green'}
							<svg class="w-4 h-4 text-green-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
						{:else if formatted.badgeColor === 'red'}
							<svg class="w-4 h-4 text-red-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						{:else}
							<svg class="w-4 h-4 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
							</svg>
						{/if}
						<!-- Hook icon (lightning bolt) -->
						<svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
							<path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
						</svg>
					{/if}
					<!-- Title -->
					<span class="text-sm font-medium text-foreground shrink-0">{formatted.title}</span>
					<!-- Status chip (task subtypes only) — muted pill, matching a tool card -->
					{#if isTaskSubtype && formatted.badge}
						<span class="text-[10px] font-medium px-1.5 py-0.5 rounded-sm border border-border bg-muted text-muted-foreground shrink-0 uppercase tracking-wide">{formatted.badge}</span>
					{/if}
					<!-- Content summary -->
					{#if formatted.content}
						<span class="text-muted-foreground shrink-0">&bull;</span>
						<span class="text-sm text-muted-foreground truncate min-w-0" title={formatted.content}>{formatted.content}</span>
					{/if}
					<!-- Chevron -->
					<svg class="w-4 h-4 text-muted-foreground ml-auto shrink-0 transition-transform group-open/hook:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</summary>
				{#if formatted.rawInput || formatted.rawOutput}
					<div class="sys-body space-y-2">
						{#if formatted.rawInput}
							<div class="p-2 bg-muted/50 rounded-sm border border-zinc-500/20">
								<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Input</div>
								<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap wrap-break-word font-mono">{formatted.rawInput}</pre>
							</div>
						{/if}
						{#if formatted.rawOutput}
							<div class="p-2 bg-muted/50 rounded-sm border border-zinc-500/20">
								<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Output</div>
								<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap wrap-break-word font-mono">{formatted.rawOutput}</pre>
							</div>
						{/if}
					</div>
				{/if}
			</details>
		</div>
	{:else if formatted.compact}
		<!-- Compact single-line rendering for hooks — styled like tool messages -->
		<div class="w-full sys-flat">
			<div class="compact-summary sys-row w-full flex items-center gap-2 min-w-0">
				<!-- Status icon matching tool message pattern -->
				{#if isProviderRetry || retryResuming}
					<!-- Still in flight: spinner, not a settled check/cross. A
					     spent card whose resume is running spins again so the
					     wait for the provider is visible. -->
					<svg class="w-4 h-4 text-primary animate-spin shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
					</svg>
				{:else if retryRecovered || formatted.badgeColor === 'green'}
					<svg class="w-4 h-4 text-green-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{:else if formatted.badgeColor === 'red'}
					<svg class="w-4 h-4 text-red-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				{:else}
					<!-- Static icon for non-terminal states (not spinner) -->
					<svg class="w-4 h-4 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{/if}
				<!-- Hook icon (lightning bolt) -->
				<svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
					<path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
				<!-- Title -->
				<span class="text-sm font-medium text-foreground shrink-0">{formatted.title}</span>
				<!-- Badge chip -->
				{#if formatted.badge}
					<span class="px-1.5 py-0.5 text-[10px] rounded-sm shrink-0 {getBadgeClasses(formatted.badgeColor || 'gray')}">
						{formatted.badge}
					</span>
				{/if}
				<!-- Content summary -->
				{#if formatted.content}
					<span class="text-muted-foreground shrink-0">&bull;</span>
					<span class="text-sm text-muted-foreground truncate min-w-0" title={formatted.content}>{formatted.content}</span>
				{/if}
				<!--
					Retry — resume the loop from the same head node, no new
					message. It stays on the card, disabled while a resume runs,
					until the session actually gets a reply back or the user
					sends something else.
				-->
				{#if canRetry}
					<button
						type="button"
						class="retry-chip ml-auto shrink-0"
						data-testid="provider-retry-button"
						disabled={retryInFlight}
						onclick={handleRetry}
					>
						{retryInFlight ? 'Retrying…' : 'Retry'}
					</button>
				{/if}
			</div>
		</div>
	{:else}
		<!--
			Generic system message. Also a row in the work log, not a stacked
			panel: icon, title, badge chip, one-line summary, chevron — the
			body lives behind a collapse and is closed by default.
		-->
		<div class="w-full sys-flat">
			<details class="group/sys">
				<summary class="sys-summary sys-row w-full flex items-center gap-2 min-w-0 cursor-pointer list-none">
					<svg class="w-4 h-4 text-primary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
					</svg>
					<span class="text-sm font-medium text-foreground shrink-0">{formatted.title}</span>
					{#if formatted.badge}
						<span class="px-1.5 py-0.5 text-[10px] rounded-sm shrink-0 {getBadgeClasses(formatted.badgeColor || 'gray')}">
							{formatted.badge}
						</span>
					{/if}
					{#if formatted.content}
						<span class="text-muted-foreground shrink-0">&bull;</span>
						<span class="text-sm text-muted-foreground truncate min-w-0" title={formatted.content}>{formatted.content}</span>
					{/if}
					<svg class="w-4 h-4 text-muted-foreground ml-auto shrink-0 transition-transform group-open/sys:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</summary>
				<div class="sys-body">
					{#if isMarkdownContent(message.systemSubtype)}
						<div class="prose prose-sm prose-invert max-w-none system-message-content">
							{@html sanitizeHtml(renderMarkdown(formatted.content))}
						</div>
					{:else}
						<pre class="text-xs text-muted-foreground overflow-x-auto whitespace-pre-wrap wrap-break-word font-mono">{formatted.content}</pre>
					{/if}
				</div>
			</details>
		</div>
	{/if}
{/if}

<style>
	/* Flat-row idiom, shared with the work log: every system event is a step in
	   the log, not a stacked panel. */
	.sys-flat {
		min-width: 0;
		margin: 0 -8px;
	}

	.sys-row {
		padding: 6px 8px;
		border-radius: 8px;
		transition: background-color 130ms ease;
	}

	.sys-row:hover {
		background: var(--scrim);
	}

	.sys-body {
		margin: 2px 0 4px 14px;
		padding-left: 12px;
		border-left: 1px solid var(--border);
	}

	/* Retry chip on a spent provider-retry row. Outlined like the rest of the
	   transcript's small controls — a hover scrim, never an opaque fill. */
	.retry-chip {
		min-height: 44px;
		padding: 0 12px;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 8px;
		cursor: pointer;
		transition: background-color 130ms ease;
	}
	.retry-chip:hover:not(:disabled) {
		background: var(--scrim);
	}
	.retry-chip:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.retry-chip:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* Generic system row: same tap target as the hook row. */
	.sys-summary {
		min-height: 44px; /* CLAUDE.md mandate: touch targets >= 44px */
	}
	.sys-summary::-webkit-details-marker {
		display: none;
	}
	.sys-summary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* Hook event row — collapsed chrome, restrained styling, mobile tap target */
	.hook-event-summary {
		min-height: 44px; /* CLAUDE.md mandate: touch targets >= 44px */
	}
	.hook-event-summary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.hook-matcher-chip {
		letter-spacing: 0.02em;
	}

	.hook-summary::-webkit-details-marker {
		display: none;
	}

	/* Custom styles for system message markdown content */
	.system-message-content :global(h2) {
		font-size: 1rem;
		font-weight: 600;
		margin-top: 0;
		margin-bottom: 0.5rem;
		color: var(--foreground);
	}
	.system-message-content :global(h3) {
		font-size: 0.875rem;
		font-weight: 600;
		margin-top: 0.75rem;
		margin-bottom: 0.25rem;
		color: var(--foreground);
	}
	.system-message-content :global(p) {
		font-size: 0.75rem;
		margin-bottom: 0.5rem;
		color: var(--muted-foreground);
	}
	.system-message-content :global(strong) {
		color: var(--foreground);
	}
	.system-message-content :global(table) {
		font-size: 0.75rem;
		width: 100%;
		border-collapse: collapse;
		margin: 0.5rem 0;
	}
	.system-message-content :global(th),
	.system-message-content :global(td) {
		padding: 0.25rem 0.5rem;
		text-align: left;
		border-bottom: 1px solid var(--border);
	}
	.system-message-content :global(th) {
		color: var(--foreground);
		font-weight: 600;
	}
	.system-message-content :global(td) {
		color: var(--muted-foreground);
	}
	.system-message-content :global(ul),
	.system-message-content :global(ol) {
		font-size: 0.75rem;
		padding-left: 1.25rem;
		margin: 0.25rem 0;
		color: var(--muted-foreground);
	}
	.system-message-content :global(li) {
		margin: 0.125rem 0;
	}
</style>
