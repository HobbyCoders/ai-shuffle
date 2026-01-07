<script lang="ts">
	import type { ChatMessage } from '$lib/stores/tabs';

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

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
					content += ` Previous context: ${preTokens.toLocaleString()} tokens.`;
				}

				return {
					title: 'Context Compacted',
					content,
					badge: trigger === 'auto' ? 'auto' : 'manual',
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

			default: {
				// For unknown subtypes, show a formatted version of the data
				return {
					title: subtype.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
					content: typeof data === 'object' ? JSON.stringify(data, null, 2) : String(data),
					badge: subtype,
					badgeColor: 'gray'
				};
			}
		}
	}

	const formatted = $derived(getFormattedContent());

	// Badge color classes
	function getBadgeClasses(color: string): string {
		switch (color) {
			case 'amber': return 'bg-amber-900/50 text-amber-300';
			case 'green': return 'bg-green-900/50 text-green-300';
			case 'blue': return 'bg-blue-900/50 text-blue-300';
			case 'purple': return 'bg-purple-900/50 text-purple-300';
			default: return 'bg-gray-900/50 text-gray-300';
		}
	}
</script>

<div class="w-full border border-border rounded-lg overflow-hidden shadow-s bg-card">
	<div class="px-4 py-2 bg-muted/30 flex items-center gap-2">
		<svg class="w-4 h-4 text-purple-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
		<pre class="text-xs text-muted-foreground overflow-x-auto whitespace-pre-wrap break-words font-mono">{formatted.content}</pre>
	</div>
</div>
