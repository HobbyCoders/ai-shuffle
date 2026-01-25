/**
 * Card System Components for The Deck
 *
 * Phase 2 implementation of the draggable window system.
 */

// Types
export * from './types';

// Components
export { default as BaseCard } from './BaseCard.svelte';
export { default as Workspace } from './Workspace.svelte';
export { default as MobileWorkspace } from './MobileWorkspace.svelte';
export { default as ChatCard } from './ChatCard.svelte';
export { default as ProfileCard } from './ProfileCard.svelte';
export { default as ProjectCard } from './ProjectCard.svelte';
export { default as SubagentCard } from './SubagentCard.svelte';
export { default as SettingsCard } from './SettingsCard.svelte';
export { default as UserSettingsCard } from './UserSettingsCard.svelte';
export { default as CardShuffle } from './CardShuffle.svelte';
export { default as StackedCardPreview } from './StackedCardPreview.svelte';

// Utility Cards
export { default as FileBrowserCard } from './FileBrowserCard.svelte';
export { default as NotesCard } from './NotesCard.svelte';

// Plugin Management
export { default as PluginManagerCard } from './PluginManagerCard.svelte';

// Hooks Management
export { default as HooksCard } from './HooksCard.svelte';

// Analytics
export { default as StatsCard } from './StatsCard.svelte';

// MCP Management
export { default as McpManagerCard } from './McpManagerCard.svelte';
