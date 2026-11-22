/**
 * Agent behavior: how every agent on this worker behaves and what it can
 * reach. Instructions (the chat instructions sent to every agent) and
 * Computer access (agents run as you in the desktop app, computer use).
 * AgentsPage.svelte renders one `<SettingRow id>` per entry. These rows were
 * on Security until the Settings reorganization; navigation.svelte.ts
 * redirects their old `security.*` anchors here.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'agents.instructions',
		category: 'agents',
		section: 'Instructions',
		label: 'Chat instructions',
		hint: 'Sent to every agent',
		keywords: ['system prompt', 'custom instructions', 'security instructions', 'default prompt', 'markdown', 'file links', 'revert']
	},
	{
		id: 'agents.runsAs',
		category: 'agents',
		section: 'Computer access',
		label: 'Agents run as you',
		hint: 'Same access as you',
		help: 'In the desktop app, agents use your own provider sign-ins, API keys and GitHub login. They can read, change and delete any file your user can, run programs and reach your network. Nothing isolates them. Run the worker in Docker or on another machine for a boundary.',
		keywords: ['disclosure', 'desktop', 'agent access', 'security'],
		platform: 'desktop-app'
	},
	{
		id: 'agents.computerUse',
		category: 'agents',
		section: 'Computer access',
		label: 'Computer use',
		help: 'Lets agents see and control this computer’s screen, mouse and keyboard, in every chat. It needs a native install with a desktop: Windows, macOS, or Linux in an X11 session. A worker in Docker or on a headless server cannot control a screen.',
		keywords: ['screen', 'mouse', 'keyboard', 'host display', 'linux', 'x11', 'wayland', 'security'],
		default: false
	},
	{
		id: 'agents.computerUseDedicated',
		category: 'agents',
		section: 'Computer access',
		label: 'Only agents use this computer',
		hint: 'Skips the first-use prompt',
		help: 'Normally the first time a chat asks to control the screen, a person must approve it, even in Bypass. Turn this on for a machine nobody works at, such as a VM for agents: the first request then follows the chat’s permission mode, so Bypass chats and automations use the screen without asking. Stop still works.',
		keywords: ['unattended', 'vm', 'virtual machine', 'dedicated', 'first use', 'computer use prompt'],
		default: false
	}
];
