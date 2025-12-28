/**
 * Chat component exports
 * Reusable chat UI components extracted from +page.svelte
 */

// Message components
export { default as UserMessage } from './UserMessage.svelte';
export { default as AssistantMessage } from './AssistantMessage.svelte';
export { default as ToolUseMessage } from './ToolUseMessage.svelte';
export { default as ToolResultMessage } from './ToolResultMessage.svelte';
export { default as StreamingIndicator } from './StreamingIndicator.svelte';

// Container components
export { default as ChatHeader } from './ChatHeader.svelte';
export { default as MessageArea } from './MessageArea.svelte';
export { default as ChatInput } from './ChatInput.svelte';
export { default as FloatingToolbar } from './FloatingToolbar.svelte';

// Banner components
export { default as PermissionBanner } from './PermissionBanner.svelte';
export { default as QuestionBanner } from './QuestionBanner.svelte';

// Re-export existing components that are already in lib/components
// These are already available from their original locations
export { default as SystemMessage } from '$lib/components/SystemMessage.svelte';
export { default as SubagentMessage } from '$lib/components/SubagentMessage.svelte';
export { default as CommandAutocomplete } from '$lib/components/CommandAutocomplete.svelte';
export { default as FileAutocomplete } from '$lib/components/FileAutocomplete.svelte';
