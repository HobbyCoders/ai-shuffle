/**
 * API client for slash commands
 */

export interface Command {
  name: string;
  display: string;
  description: string;
  argument_hint?: string;
  type: 'custom' | 'interactive' | 'sdk_builtin' | 'plugin';
  source?: string;
  namespace?: string;
}

export interface CommandListResponse {
  commands: Command[];
  count: number;
}

export interface CommandDetailResponse {
  name: string;
  display: string;
  description: string;
  content?: string;
  argument_hint?: string;
  type: string;
  source?: string;
  namespace?: string;
  allowed_tools: string[];
  model?: string;
  is_interactive: boolean;
}

export interface ExecuteCommandResponse {
  success: boolean;
  message: string;
  expanded_prompt?: string;
  is_interactive: boolean;
}

export interface SyncAfterRewindResponse {
  success: boolean;
  message: string;
  deleted_count: number;
  checkpoint_index?: number;
}

/**
 * Fetch all available commands
 */
export async function listCommands(projectId?: string): Promise<CommandListResponse> {
  const params = new URLSearchParams();
  if (projectId) {
    params.set('project_id', projectId);
  }

  const response = await fetch(`/api/v1/commands/?${params}`, {
    credentials: 'include'
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch commands: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get details for a specific command
 */
export async function getCommand(commandName: string, projectId?: string): Promise<CommandDetailResponse> {
  const params = new URLSearchParams();
  if (projectId) {
    params.set('project_id', projectId);
  }

  const response = await fetch(`/api/v1/commands/${commandName}?${params}`, {
    credentials: 'include'
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch command: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Execute a custom command
 */
export async function executeCommand(command: string, sessionId: string): Promise<ExecuteCommandResponse> {
  const response = await fetch('/api/v1/commands/execute', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    credentials: 'include',
    body: JSON.stringify({ command, session_id: sessionId })
  });

  if (!response.ok) {
    throw new Error(`Failed to execute command: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Sync chat after rewind completes
 */
export async function syncAfterRewind(
  sessionId: string,
  checkpointMessage: string,
  restoreOption: number
): Promise<SyncAfterRewindResponse> {
  const params = new URLSearchParams({
    session_id: sessionId,
    checkpoint_message: checkpointMessage,
    restore_option: restoreOption.toString()
  });

  const response = await fetch(`/api/v1/commands/sync-after-rewind?${params}`, {
    method: 'POST',
    credentials: 'include'
  });

  if (!response.ok) {
    throw new Error(`Failed to sync after rewind: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Check if input is a slash command
 */
export function isSlashCommand(input: string): boolean {
  return input.startsWith('/') && input.length > 1;
}

/**
 * Parse command input into name and arguments
 */
export function parseCommandInput(input: string): { name: string; args: string } {
  if (!isSlashCommand(input)) {
    return { name: '', args: input };
  }

  const withoutSlash = input.slice(1);
  const spaceIndex = withoutSlash.indexOf(' ');

  if (spaceIndex === -1) {
    return { name: withoutSlash, args: '' };
  }

  return {
    name: withoutSlash.slice(0, spaceIndex),
    args: withoutSlash.slice(spaceIndex + 1)
  };
}
