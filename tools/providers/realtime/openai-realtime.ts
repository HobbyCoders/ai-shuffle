/**
 * OpenAI Realtime Voice Provider
 *
 * Adapter for OpenAI's Realtime API for live speech-to-speech conversations.
 * Creates sessions that can be used with WebSocket connections.
 *
 * NOTE: This provider creates session tokens but does NOT handle the WebSocket
 * connection itself. The frontend/client must implement the WebSocket handling.
 *
 * API Reference: https://platform.openai.com/docs/api-reference/realtime
 */

import {
  RealtimeVoiceProvider,
  RealtimeSessionResult,
  RealtimeSessionConfig,
  ProviderCredentials,
} from '../types.js';

// ============================================================================
// Provider Implementation
// ============================================================================

export const openaiRealtimeProvider: RealtimeVoiceProvider = {
  id: 'openai-realtime',
  name: 'OpenAI Realtime Voice',
  models: [
    {
      id: 'gpt-4o-realtime-preview',
      name: 'GPT-4o Realtime Preview',
      description: 'Real-time speech-to-speech with GPT-4o quality',
      capabilities: ['realtime-voice'],
      pricing: { unit: 'minute', price: 0.10 }
    },
    {
      id: 'gpt-4o-mini-realtime-preview',
      name: 'GPT-4o Mini Realtime Preview',
      description: 'Faster, more affordable realtime voice',
      capabilities: ['realtime-voice'],
      pricing: { unit: 'minute', price: 0.02 }
    }
  ],

  async createSession(
    config: RealtimeSessionConfig,
    credentials: ProviderCredentials,
    model: string
  ): Promise<RealtimeSessionResult> {
    const apiKey = credentials.apiKey;

    try {
      // Build session configuration
      const sessionConfig: any = {
        model: model,
        voice: config.voice || 'alloy'
      };

      // Add optional configuration
      if (config.instructions) {
        sessionConfig.instructions = config.instructions;
      }

      if (config.input_audio_format) {
        sessionConfig.input_audio_format = config.input_audio_format;
      }

      if (config.output_audio_format) {
        sessionConfig.output_audio_format = config.output_audio_format;
      }

      if (config.input_audio_transcription !== undefined) {
        sessionConfig.input_audio_transcription = {
          model: 'whisper-1'
        };
      }

      if (config.turn_detection) {
        if (config.turn_detection === 'server_vad') {
          sessionConfig.turn_detection = {
            type: 'server_vad',
            threshold: 0.5,
            prefix_padding_ms: 300,
            silence_duration_ms: 500
          };
        } else {
          sessionConfig.turn_detection = null;
        }
      }

      if (config.temperature !== undefined) {
        sessionConfig.temperature = config.temperature;
      }

      // Available voices: alloy, ash, ballad, coral, echo, sage, shimmer, verse
      const validVoices = ['alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse'];
      if (sessionConfig.voice && !validVoices.includes(sessionConfig.voice)) {
        return {
          success: false,
          error: `Invalid voice. Available voices: ${validVoices.join(', ')}`
        };
      }

      // Create ephemeral token for client-side WebSocket connection
      const response = await fetch('https://api.openai.com/v1/realtime/sessions', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(sessionConfig)
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({})) as any;
        const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;

        if (response.status === 401) {
          return { success: false, error: 'API key is invalid or expired.' };
        }
        if (response.status === 403) {
          return { success: false, error: 'API key does not have access to Realtime API.' };
        }
        if (response.status === 429) {
          return { success: false, error: 'Rate limit exceeded. Please wait and try again.' };
        }
        return { success: false, error: `API error: ${errorMsg}` };
      }

      const result = await response.json() as any;

      return {
        success: true,
        session_id: result.id,
        // The client should connect to this WebSocket URL with the token
        websocket_url: 'wss://api.openai.com/v1/realtime',
        token: result.client_secret?.value,
        expires_at: result.client_secret?.expires_at
          ? new Date(result.client_secret.expires_at * 1000).toISOString()
          : undefined
      };

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      const response = await fetch('https://api.openai.com/v1/models', {
        method: 'GET',
        headers: {
          'Authorization': `Bearer ${credentials.apiKey}`
        }
      });

      if (response.status === 401) {
        return { valid: false, error: 'Invalid API key' };
      }

      if (response.status === 403) {
        return { valid: false, error: 'API key does not have sufficient permissions' };
      }

      if (!response.ok) {
        return { valid: false, error: `API returned status ${response.status}` };
      }

      return { valid: true };
    } catch (error) {
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default openaiRealtimeProvider;
