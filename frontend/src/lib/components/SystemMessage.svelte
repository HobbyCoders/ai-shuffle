<script lang="ts">
	import type { ChatMessage } from '$lib/stores/types';
	import { Marked } from 'marked';
	import { sanitizeHtml } from '$lib/utils/sanitize';

	// Create a local marked instance to avoid mutating global config
	const localMarked = new Marked({ breaks: true, gfm: true });

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	// Check if content should be rendered as markdown
	function isMarkdownContent(subtype: string | undefined): boolean {
		return ['local_command'].includes(subtype || '');
	}

	function renderMarkdown(content: string): string {
		return localMarked.parse(content, { breaks: true }) as string;
	}

	// Get formatted content based on subtype
	function getFormattedContent(): { title: string; content: string; badge?: string; badgeColor?: string; hidden?: boolean; compact?: boolean; collapsible?: boolean; rawOutput?: string } {
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
						dontAsk: 'Auto-Approve'
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
				// Hook completion — collapsible with raw output
				const exitCode = (data.exitCode as number) ?? (data.exit_code as number) ?? null;
				// Default to success if no explicit error indicators
				const hasError = exitCode !== null && exitCode !== 0;
				const explicitFail = (data.success as boolean) === false;
				const success = !hasError && !explicitFail;
				const errMsg = (data.error as string) || (data.stderr as string) || '';
				const outMsg = (data.output as string) || (data.stdout as string) || (data.message as string) || '';
				const hookNameResp = (data.hook_name as string) || (data.hookName as string) || (data.name as string) || '';
				const rawOutput = errMsg || outMsg;
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
				// Task/subagent launch notifications - hide as they're internal SDK events
				return {
					title: 'Task Started',
					content: (data.description as string) || message.content || '',
					badge: 'task_started',
					badgeColor: 'gray',
					hidden: true,
					compact: true
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

{#if !formatted.hidden}
	{#if formatted.collapsible}
		<!-- Collapsible rendering for hook responses — styled like tool messages -->
		<div class="w-full border border-border rounded-lg overflow-hidden">
			<details class="group/hook">
				<summary class="hook-summary w-full px-3 py-2 flex items-center gap-2 min-w-0 cursor-pointer list-none">
					<!-- Status icon -->
					{#if formatted.badgeColor === 'green'}
						<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					{:else if formatted.badgeColor === 'red'}
						<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					{:else}
						<svg class="w-4 h-4 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					{/if}
					<!-- Hook icon (lightning bolt) -->
					<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
						<path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
					</svg>
					<!-- Title -->
					<span class="text-sm font-medium text-foreground flex-shrink-0">{formatted.title}</span>
					<!-- Content summary -->
					{#if formatted.content}
						<span class="text-muted-foreground flex-shrink-0">&bull;</span>
						<span class="text-sm text-muted-foreground truncate min-w-0" title={formatted.content}>{formatted.content}</span>
					{/if}
					<!-- Chevron -->
					<svg class="w-4 h-4 text-muted-foreground ml-auto flex-shrink-0 transition-transform group-open/hook:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</summary>
				{#if formatted.rawOutput}
					<div class="px-3 pb-3 pt-1">
						<div class="p-2 bg-muted/50 rounded border border-zinc-500/20">
							<div class="text-xs text-muted-foreground/70 mb-1 font-medium">Output</div>
							<pre class="text-xs text-muted-foreground overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">{formatted.rawOutput}</pre>
						</div>
					</div>
				{/if}
			</details>
		</div>
	{:else if formatted.compact}
		<!-- Compact single-line rendering for hooks — styled like tool messages -->
		<div class="w-full border border-border rounded-lg overflow-hidden">
			<div class="compact-summary w-full px-3 py-2 flex items-center gap-2 min-w-0">
				<!-- Status icon matching tool message pattern -->
				{#if formatted.badgeColor === 'green'}
					<svg class="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{:else if formatted.badgeColor === 'red'}
					<svg class="w-4 h-4 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				{:else}
					<!-- Static icon for non-terminal states (not spinner) -->
					<svg class="w-4 h-4 text-muted-foreground flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{/if}
				<!-- Hook icon (lightning bolt) -->
				<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
					<path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
				<!-- Title -->
				<span class="text-sm font-medium text-foreground flex-shrink-0">{formatted.title}</span>
				<!-- Content summary -->
				{#if formatted.content}
					<span class="text-muted-foreground flex-shrink-0">&bull;</span>
					<span class="text-sm text-muted-foreground truncate min-w-0" title={formatted.content}>{formatted.content}</span>
				{/if}
			</div>
		</div>
	{:else}
		<!-- Full card rendering for important system messages -->
		<div class="w-full border border-border rounded-lg overflow-hidden shadow-s bg-card">
			<div class="px-4 py-2 bg-muted/30 flex items-center gap-2">
				<svg class="w-4 h-4 text-primary flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
				<span class="text-sm font-medium text-foreground">{formatted.title}</span>
				{#if formatted.badge}
					<span class="px-1.5 py-0.5 text-xs rounded {getBadgeClasses(formatted.badgeColor || 'gray')}">
						{formatted.badge}
					</span>
				{/if}
			</div>
			<div class="px-4 py-3 bg-card">
				{#if isMarkdownContent(message.systemSubtype)}
					<div class="prose prose-sm prose-invert max-w-none system-message-content">
						{@html sanitizeHtml(renderMarkdown(formatted.content))}
					</div>
				{:else}
					<pre class="text-xs text-muted-foreground overflow-x-auto whitespace-pre-wrap break-words font-mono">{formatted.content}</pre>
				{/if}
			</div>
		</div>
	{/if}
{/if}

<style>
	/* Compact hook messages — match tool-summary hover behavior */
	.compact-summary {
		transition: background-color 150ms ease;
	}
	/* Collapsible hook summary — match tool message details pattern */
	.hook-summary {
		transition: background-color 150ms ease;
	}
	.hook-summary:hover {
		background-color: hsl(var(--muted) / 0.5);
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
