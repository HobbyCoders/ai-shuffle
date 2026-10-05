/**
 * Security: owner password, trusted LAN, agents run as you (desktop app),
 * computer use, code sandbox, chat instructions.
 * SecurityPage.svelte renders one `<SettingRow id>` per entry.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'security.ownerPassword',
		category: 'security',
		section: 'Sign-in',
		label: 'Owner password',
		keywords: ['password', 'change', 'set', 'owner access', 'sign in']
	},
	{
		id: 'security.trustedLan',
		category: 'security',
		section: 'Sign-in',
		label: 'Trusted LAN, no login',
		hint: 'Network devices skip password',
		help: 'Any browser that can reach this address becomes the owner without a password: guests on your Wi-Fi, other people on your tailnet, and any compromised device. Leave this off unless your network is only yours.',
		keywords: ['tailnet', 'lan', 'wifi', 'no password'],
		default: false
	},
	{
		id: 'security.runsAs',
		category: 'security',
		section: 'Agent safety',
		label: 'Agents run as you',
		hint: 'Same access as you',
		help: 'In the desktop app, agents use your own provider sign-ins, API keys and GitHub login. They can read, change and delete any file your user can, run programs and reach your network. Nothing isolates them. Run the worker in Docker or on another machine for a boundary.',
		keywords: ['disclosure', 'desktop', 'agent access'],
		platform: 'desktop-app'
	},
	{
		id: 'security.computerUse',
		category: 'security',
		section: 'Agent safety',
		label: 'Computer use',
		help: 'Lets agents see and control this computer’s screen, mouse and keyboard, in every chat. It needs a native install with a desktop: Windows, macOS, or Linux in an X11 session. A worker in Docker or on a headless server cannot control a screen.',
		keywords: ['screen', 'mouse', 'keyboard', 'host display', 'linux', 'x11', 'wayland'],
		default: false
	},
	{
		id: 'security.computerUseDedicated',
		category: 'security',
		section: 'Agent safety',
		label: 'Only agents use this computer',
		hint: 'Skips the first-use prompt',
		help: 'Normally the first time a chat asks to control the screen, a person must approve it, even in Bypass. Turn this on for a machine nobody works at, such as a VM for agents: the first request then follows the chat’s permission mode, so Bypass chats and automations use the screen without asking. Stop still works.',
		keywords: ['unattended', 'vm', 'virtual machine', 'dedicated', 'first use', 'computer use prompt'],
		default: false
	},
	{
		id: 'security.sandbox',
		category: 'security',
		section: 'Agent safety',
		label: 'Code sandbox',
		help: 'Isolates agents’ Python from this worker’s files and network with bubblewrap, Linux only. When it is off, Ask mode prompts before each Python cell; Bypass still asks for explicit consent requests.',
		keywords: ['python', 'bubblewrap', 'isolation', 'code mode']
	},
	{
		id: 'security.secInstr',
		category: 'security',
		section: 'Agent safety',
		label: 'Chat instructions',
		hint: 'Sent to every agent',
		keywords: ['system prompt', 'custom instructions', 'security instructions', 'markdown', 'file links', 'revert']
	}
];
