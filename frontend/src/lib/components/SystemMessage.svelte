<script lang="ts">
	import type { ChatMessage } from '$lib/stores/tabs';
	import { marked } from 'marked';

	// Configure marked for system message rendering
	marked.setOptions({
		breaks: true,
		gfm: true
	});

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	// Check if content should be rendered as markdown
	function isMarkdownContent(subtype: string | undefined): boolean {
		return subtype === 'local_command';
	}

	function renderMarkdown(content: string): string {
		return marked(content, { breaks: true }) as string;
	}

	// Get formatted content based on subtype
	function getFormattedContent(): { title: string; content: string; badge?: string; badgeColor?: string } {
		const subtype = message.systemSubtype || 'system';
		const data = message.systemData || {};

		switch (subtype) {
			case 'status': {
				const status = data.status as string || 'unknown';
				if (status === 'compacting') {
					return {
						title: 'Auto Compacting',
						content: 'Context is being automatically compacted to free up space...',
						badge: 'compacting',
						badgeColor: 'amber'
					};
				}
				return {
					title: 'Status',
					content: status || 'Status update',
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
				// These are synthetic messages with isApiErrorMessage: true
				const apiErrorContent = message.content || 'An API error occurred';
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
				// For unknown subtypes, show a formatted version of the data
				// Check if it looks like an error based on subtype name
				const isError = subtype.includes('error') || subtype.includes('fail') || subtype.includes('exception');
				return {
					title: subtype.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
					content: typeof data === 'object' ? JSON.stringify(data, null, 2) : String(data),
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
				{@html renderMarkdown(formatted.content)}
			</div>
		{:else}
			<pre class="text-xs text-muted-foreground overflow-x-auto whitespace-pre-wrap break-words font-mono">{formatted.content}</pre>
		{/if}
	</div>
</div>

<style>
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
