/**
 * How Settings words a plugin's parts: readable kind names, what a plugin
 * runs once switched on (hooks and MCP servers), what was adapted when it was
 * read, what was left out, and which outside services it declares.
 *
 * Pure functions over the API's `components`, so the wording is tested here
 * and every pane says the same thing.
 */
import type { PluginComponent } from '$lib/api/plugins';

/** The label a plugin waiting for the owner carries everywhere. */
export const NEEDS_REVIEW = 'Needs your review';

const COMPONENT_LABELS: Record<string, string> = {
	skill: 'Skill',
	command: 'Command',
	agent: 'Agent',
	mcp_server: 'MCP server',
	hook: 'Hook',
	app: 'ChatGPT app',
	lsp_server: 'Language server',
	output_style: 'Output style',
	user_config: 'Plugin settings',
	dependency: 'Dependencies',
	connector_runtime: 'Connector runtime',
	service: 'Outside service',
	executables: 'Programs',
	monitor: 'Monitor',
	theme: 'Theme',
	workflow: 'Workflow',
	channel: 'Message channel',
	settings: 'Default settings'
};

/** A readable name for a component kind. An unknown kind is spelled out: `foo_bar` reads "Foo bar". */
export function componentLabel(kind: string): string {
	const known = COMPONENT_LABELS[kind];
	if (known) return known;
	const words = kind
		.replace(/([a-z])([A-Z])/g, '$1 $2')
		.replace(/[_\-.]+/g, ' ')
		.trim()
		.toLowerCase();
	return words ? words[0].toUpperCase() + words.slice(1) : 'Other part';
}

const HOOK_EVENTS: Record<string, string> = {
	PreToolUse: 'Before a tool runs',
	PostToolUse: 'After a tool runs',
	PostToolUseFailure: 'After a tool fails',
	UserPromptSubmit: 'When you send a message',
	SessionStart: 'When a chat starts',
	SessionEnd: 'When a chat ends',
	Stop: 'When the agent finishes',
	SubagentStart: 'When a subagent starts',
	SubagentStop: 'When a subagent finishes',
	Notification: 'On a notification',
	PreCompact: 'Before the chat is compacted',
	PermissionRequest: 'When a permission is asked'
};

/** When a hook event fires, in plain words. */
export function hookEventText(event: string): string {
	return HOOK_EVENTS[event] ?? `On the ${event} event`;
}

/** One thing a plugin runs once it is on, said plainly. */
export interface RunsCode {
	key: string;
	/** "Runs a program", "Connects to a server", or when a hook fires. */
	what: string;
	/** The server or hook event name. */
	name: string;
	/** Command line, URL or the hook's commands; empty when the backend sent none. */
	detail: string;
}

/**
 * What a plugin will run once switched on: its hooks and MCP servers that
 * load here. Parts that do not load run nothing, so they are left out.
 */
export function runsCode(components: PluginComponent[] | null | undefined): RunsCode[] {
	const out: RunsCode[] = [];
	for (const c of components ?? []) {
		if (c.support !== 'loaded') continue;
		const detail = (c.detail ?? '').trim();
		if (c.kind === 'mcp_server') {
			const remote = /^https?:\/\//i.test(detail);
			out.push({
				key: `mcp:${c.name}`,
				what: remote ? 'Connects to a server' : 'Runs a program',
				name: c.name,
				detail
			});
		} else if (c.kind === 'hook') {
			out.push({ key: `hook:${c.name}`, what: hookEventText(c.name), name: c.name, detail });
		}
	}
	return out;
}

/** A component and the sentence about it. */
export interface ComponentNote {
	key: string;
	label: string;
	note: string;
}

function noteOf(c: PluginComponent, note: string): ComponentNote {
	return { key: `${c.kind}:${c.name}`, label: `${componentLabel(c.kind)} ${c.name}`, note };
}

/**
 * Loaded parts that were changed to work here: a model pin removed, a tool
 * mapped or dropped. The backend puts that on a loaded component's `reason`;
 * an empty reason means nothing was changed.
 */
export function adaptedParts(components: PluginComponent[] | null | undefined): ComponentNote[] {
	return (components ?? [])
		.filter((c) => c.support === 'loaded' && c.kind !== 'service' && c.reason?.trim())
		.map((c) => noteOf(c, c.reason.trim()));
}

/** Parts that do not load here, each with why. */
export function leftOutParts(components: PluginComponent[] | null | undefined): ComponentNote[] {
	return (components ?? [])
		.filter((c) => c.support !== 'loaded' && c.kind !== 'service')
		.map((c) => noteOf(c, c.reason?.trim() || 'Not supported here'));
}

/** Names of the outside services a plugin declares it needs. */
export function declaredServices(components: PluginComponent[] | null | undefined): string[] {
	return [...new Set((components ?? []).filter((c) => c.kind === 'service' && c.name).map((c) => c.name))];
}

/**
 * A source name from its git URL: the last path segment without `.git`
 * (`https://github.com/acme/team-plugins.git` reads `team-plugins`).
 */
export function sourceNameFromUrl(url: string): string {
	const path = url
		.trim()
		.replace(/[?#].*$/, '')
		.replace(/\/+$/, '');
	const last = path.split(/[/:]/).filter(Boolean).pop() ?? '';
	// The backend accepts letters, digits, `_` and `-` only.
	return last
		.replace(/\.git$/i, '')
		.replace(/[^A-Za-z0-9_-]+/g, '-')
		.replace(/^-+|-+$/g, '');
}
