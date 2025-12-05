<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	export interface PermissionRequestData {
		request_id: string;
		tool_name: string;
		tool_input: Record<string, unknown>;
		queue_position?: number;
		queue_total?: number;
	}

	interface Props {
		request: PermissionRequestData;
		isFirst?: boolean;
		queueCount?: number;
	}

	let { request, isFirst = true, queueCount = 1 }: Props = $props();

	const dispatch = createEventDispatcher<{
		respond: {
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		};
	}>();

	let showAdvanced = $state(false);
	let rememberChoice = $state<'none' | 'session' | 'profile'>('none');
	let customPattern = $state('');

	// Tool display info
	const toolInfo: Record<string, { icon: string; color: string; description: string }> = {
		Bash: {
			icon: '💻',
			color: 'text-green-400',
			description: 'Execute a shell command'
		},
		Read: {
			icon: '📖',
			color: 'text-blue-400',
			description: 'Read file contents'
		},
		Write: {
			icon: '✏️',
			color: 'text-yellow-400',
			description: 'Create or overwrite a file'
		},
		Edit: {
			icon: '📝',
			color: 'text-orange-400',
			description: 'Edit file contents'
		},
		Glob: {
			icon: '🔍',
			color: 'text-purple-400',
			description: 'Search for files by pattern'
		},
		Grep: {
			icon: '🔎',
			color: 'text-purple-400',
			description: 'Search file contents'
		},
		WebFetch: {
			icon: '🌐',
			color: 'text-cyan-400',
			description: 'Fetch URL content'
		},
		WebSearch: {
			icon: '🔍',
			color: 'text-cyan-400',
			description: 'Search the web'
		},
		Task: {
			icon: '🤖',
			color: 'text-indigo-400',
			description: 'Launch a subagent'
		},
		NotebookEdit: {
			icon: '📓',
			color: 'text-amber-400',
			description: 'Edit Jupyter notebook'
		}
	};

	function getToolInfo(name: string) {
		return (
			toolInfo[name] || {
				icon: '🔧',
				color: 'text-gray-400',
				description: 'Use tool'
			}
		);
	}

	// Format tool input for display
	function formatToolInput(input: Record<string, unknown>): { key: string; value: string; truncated: boolean }[] {
		const result: { key: string; value: string; truncated: boolean }[] = [];

		for (const [key, value] of Object.entries(input)) {
			let displayValue: string;
			let truncated = false;

			if (typeof value === 'string') {
				if (value.length > 200) {
					displayValue = value.substring(0, 200) + '...';
					truncated = true;
				} else {
					displayValue = value;
				}
			} else if (typeof value === 'object' && value !== null) {
				const json = JSON.stringify(value, null, 2);
				if (json.length > 200) {
					displayValue = json.substring(0, 200) + '...';
					truncated = true;
				} else {
					displayValue = json;
				}
			} else {
				displayValue = String(value);
			}

			result.push({ key, value: displayValue, truncated });
		}

		return result;
	}

	// Get suggested pattern based on tool type
	function getSuggestedPattern(): string {
		const input = request.tool_input;

		switch (request.tool_name) {
			case 'Bash': {
				const cmd = (input.command as string) || '';
				// Suggest pattern based on command prefix
				const firstWord = cmd.split(' ')[0];
				return `${firstWord} *`;
			}
			case 'Read':
			case 'Write':
			case 'Edit': {
				const path = (input.file_path as string) || '';
				// Suggest directory pattern
				const dir = path.substring(0, path.lastIndexOf('/'));
				return dir ? `${dir}/*` : '*';
			}
			case 'Glob':
			case 'Grep': {
				const path = (input.path as string) || '';
				return path || '*';
			}
			case 'WebFetch': {
				const url = (input.url as string) || '';
				try {
					const parsed = new URL(url);
					return `${parsed.origin}/*`;
				} catch {
					return '*';
				}
			}
			default:
				return '*';
		}
	}

	function handleAllow() {
		dispatch('respond', {
			request_id: request.request_id,
			decision: 'allow',
			remember: rememberChoice,
			pattern: rememberChoice !== 'none' ? (customPattern || getSuggestedPattern()) : undefined
		});
	}

	function handleDeny() {
		dispatch('respond', {
			request_id: request.request_id,
			decision: 'deny',
			remember: rememberChoice,
			pattern: rememberChoice !== 'none' ? (customPattern || getSuggestedPattern()) : undefined
		});
	}

	function handleAllowAll() {
		// Allow with "always" scope - will auto-resolve matching queued requests
		dispatch('respond', {
			request_id: request.request_id,
			decision: 'allow',
			remember: 'profile',
			pattern: getSuggestedPattern()
		});
	}

	const info = $derived(getToolInfo(request.tool_name));
	const formattedInput = $derived(formatToolInput(request.tool_input));
	const suggestedPattern = $derived(getSuggestedPattern());
</script>

<div class="border border-amber-500/50 rounded-lg overflow-hidden shadow-lg bg-card animate-pulse-border">
	<!-- Header -->
	<div class="px-4 py-3 bg-amber-900/20 flex items-center justify-between">
		<div class="flex items-center gap-2">
			<span class="text-lg">{info.icon}</span>
			<span class="text-sm font-medium {info.color}">{request.tool_name}</span>
			<span class="text-xs text-muted-foreground">- {info.description}</span>
		</div>
		{#if queueCount > 1}
			<span class="px-2 py-0.5 text-xs rounded bg-amber-900/50 text-amber-300">
				{queueCount} pending
			</span>
		{/if}
	</div>

	<!-- Tool Input Details -->
	<div class="px-4 py-3 bg-card border-b border-border">
		<div class="space-y-2">
			{#each formattedInput as { key, value, truncated }}
				<div class="text-sm">
					<span class="text-muted-foreground font-medium">{key}:</span>
					<pre class="mt-1 p-2 bg-muted/30 rounded text-xs text-foreground overflow-x-auto whitespace-pre-wrap break-all font-mono">{value}</pre>
					{#if truncated}
						<span class="text-xs text-muted-foreground italic">...truncated</span>
					{/if}
				</div>
			{/each}
		</div>
	</div>

	<!-- Advanced Options (collapsible) -->
	{#if showAdvanced}
		<div class="px-4 py-3 bg-muted/20 border-b border-border space-y-3">
			<div class="text-xs text-muted-foreground">Remember this decision:</div>
			<div class="flex flex-wrap gap-2">
				<label class="flex items-center gap-1.5 cursor-pointer">
					<input
						type="radio"
						name="remember-{request.request_id}"
						value="none"
						bind:group={rememberChoice}
						class="w-3 h-3"
					/>
					<span class="text-xs">Just this once</span>
				</label>
				<label class="flex items-center gap-1.5 cursor-pointer">
					<input
						type="radio"
						name="remember-{request.request_id}"
						value="session"
						bind:group={rememberChoice}
						class="w-3 h-3"
					/>
					<span class="text-xs">This session</span>
				</label>
				<label class="flex items-center gap-1.5 cursor-pointer">
					<input
						type="radio"
						name="remember-{request.request_id}"
						value="profile"
						bind:group={rememberChoice}
						class="w-3 h-3"
					/>
					<span class="text-xs">Always (profile)</span>
				</label>
			</div>

			{#if rememberChoice !== 'none'}
				<div class="space-y-1">
					<label class="text-xs text-muted-foreground">Pattern to match:</label>
					<input
						type="text"
						bind:value={customPattern}
						placeholder={suggestedPattern}
						class="w-full px-2 py-1.5 text-xs bg-input border border-border rounded focus:outline-none focus:ring-1 focus:ring-amber-500 font-mono"
					/>
					<div class="text-xs text-muted-foreground">
						Use * as wildcard. Leave empty for suggested: <code class="text-amber-400">{suggestedPattern}</code>
					</div>
				</div>
			{/if}
		</div>
	{/if}

	<!-- Actions -->
	<div class="px-4 py-3 bg-card flex items-center justify-between gap-2">
		<button
			type="button"
			onclick={() => (showAdvanced = !showAdvanced)}
			class="text-xs text-muted-foreground hover:text-foreground transition-colors"
		>
			{showAdvanced ? 'Hide' : 'More'} options
		</button>

		<div class="flex items-center gap-2">
			{#if queueCount > 1 && isFirst}
				<button
					type="button"
					onclick={handleAllowAll}
					class="px-3 py-1.5 text-xs font-medium bg-green-600/20 text-green-400 border border-green-600/50 rounded hover:bg-green-600/30 transition-colors"
				>
					Allow All Similar
				</button>
			{/if}
			<button
				type="button"
				onclick={handleDeny}
				class="px-4 py-1.5 text-sm font-medium bg-red-600/20 text-red-400 border border-red-600/50 rounded hover:bg-red-600/30 transition-colors"
			>
				Deny
			</button>
			<button
				type="button"
				onclick={handleAllow}
				class="px-4 py-1.5 text-sm font-medium bg-green-600/20 text-green-400 border border-green-600/50 rounded hover:bg-green-600/30 transition-colors"
			>
				Allow
			</button>
		</div>
	</div>
</div>

<style>
	@keyframes pulse-border {
		0%,
		100% {
			border-color: rgba(245, 158, 11, 0.5);
		}
		50% {
			border-color: rgba(245, 158, 11, 0.8);
		}
	}

	.animate-pulse-border {
		animation: pulse-border 2s ease-in-out infinite;
	}
</style>
