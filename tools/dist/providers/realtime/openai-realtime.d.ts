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
import { RealtimeVoiceProvider } from '../types.js';
export declare const openaiRealtimeProvider: RealtimeVoiceProvider;
export default openaiRealtimeProvider;
//# sourceMappingURL=openai-realtime.d.ts.map