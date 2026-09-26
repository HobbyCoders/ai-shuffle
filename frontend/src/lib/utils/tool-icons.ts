/**
 * Tool icon + display-name mapping, shared by every surface that renders a tool
 * call.
 *
 * It used to live inside `ToolUseMessage.svelte`, which meant the SUBAGENT card
 * had no access to it and drew every child row with one hard-coded cog. Both
 * lanes now read the same table, so a Read inside a subagent looks like a Read
 * in the main feed.
 */

export type IconType =
	| 'image'
	| 'video'
	| 'cube'
	| 'file'
	| 'pencil'
	| 'search'
	| 'terminal'
	| 'globe'
	| 'bot'
	| 'list'
	| 'cog'
	| 'clock'
	| 'bell'
	| 'eye'
	| 'branch'
	| 'stop'
	| 'send';

const TOOL_ICON_MAP: Record<string, IconType> = {
	// Image generation
	generateImage: 'image', editImage: 'image', generateWithReference: 'image',
	// Video generation
	generateVideo: 'video', imageToVideo: 'video', bridgeFrames: 'video', extendVideo: 'video', analyzeVideo: 'video',
	// 3D generation
	textTo3D: 'cube', imageTo3D: 'cube', retexture3D: 'cube', rig3D: 'cube', animate3D: 'cube', getTask3D: 'cube',
	// File operations
	Read: 'file', Write: 'pencil', Edit: 'pencil', NotebookEdit: 'pencil', Note: 'pencil',
	// Search
	Glob: 'search', Grep: 'search', ToolSearch: 'search',
	// Transcript / recall builtins (fluid)
	HistorySearch: 'search', SessionSearch: 'search', SessionView: 'clock',
	HistoryExpand: 'clock',
	Ledger: 'list', AskUser: 'bot',
	// Terminal
	Bash: 'terminal', KillShell: 'terminal', SlashCommand: 'terminal', LSP: 'terminal',
	// Web
	WebFetch: 'globe', WebSearch: 'globe',
	// Agent / Skills
	Task: 'bot', Skill: 'bot', Agent: 'bot', 'Agent message': 'bot', Swarm: 'branch',
	// Todo
	TodoWrite: 'list',
	// Questions
	AskUserQuestion: 'bot',
	// Scheduling
	ScheduleWakeup: 'clock', CronCreate: 'clock', CronList: 'clock', CronDelete: 'clock',
	RemoteTrigger: 'send',
	// Worktree
	EnterWorktree: 'branch', ExitWorktree: 'branch',
	// Monitor / background tasks
	Monitor: 'eye', TaskOutput: 'eye', TaskStop: 'stop', TaskList: 'list', TaskGet: 'list',
	// Notifications
	NotifyDevices: 'bell',
	// Legacy alias kept so historical transcript cards keep their icon
	PushNotification: 'bell',
};

/**
 * The bare tool name, with any `mcp__<server>__` prefix stripped.
 *
 * MCP-bridged app builtins arrive as `mcp__builtins__TaskGet`, which is a wire
 * name, not something to show a human. The server segment is dropped rather
 * than kept as a "builtins: " prefix because the app has exactly one bridged
 * server the user ever sees named, and the raw name is still available as the
 * element's `title`. Names that are not MCP-prefixed pass through untouched.
 */
export function displayToolName(toolName: string): string {
	if (!toolName?.startsWith('mcp__')) return toolName ?? '';
	const parts = toolName.split('__');
	// mcp__<server>__<tool>; a tool whose own name contains `__` keeps it.
	return parts.length >= 3 ? parts.slice(2).join('__') : toolName;
}

/** The icon bucket for a tool name (MCP-prefixed names resolve on their suffix). */
export function getToolIconType(toolName: string): IconType {
	// Direct lookup first
	if (TOOL_ICON_MAP[toolName]) return TOOL_ICON_MAP[toolName];
	// Handle MCP-prefixed names (e.g., mcp__browser-tools__browser_navigate → browser_navigate)
	const mcpSuffix = displayToolName(toolName);
	if (mcpSuffix && TOOL_ICON_MAP[mcpSuffix]) return TOOL_ICON_MAP[mcpSuffix];
	return 'cog';
}
