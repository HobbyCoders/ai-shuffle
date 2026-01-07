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
