"""
Realtime voice pipeline (Pipecat + WebRTC).

Architecture:
    Browser (SvelteKit + @pipecat-ai/client-js)
        ↓ WebRTC (SmallWebRTCTransport)
    FastAPI /api/v1/voice/realtime/offer
        ↓ Pipecat pipeline
    [transport.input → Silero VAD → SmartTurnAnalyzer(optional)
     → STT(Deepgram|Groq) → ClaudeAgentLLMService → TTS(Cartesia|ElevenLabs)
     → transport.output]
        ↓
    existing Claude Agent SDK (tools, MCP, worktree) [Phase 2]

Modules:
    pipeline      — Pipecat pipeline factory
    providers     — STT/TTS/LLM provider resolution from system_settings
    transport     — SmallWebRTCTransport setup + SDP handling
    session       — VoiceSession lifecycle (start/stop/cleanup)
    claude_llm    — (Phase 2) custom Pipecat LLMService bridging to SessionPool
    pricing       — (Phase 3) cost tracking per provider
"""

from app.voice.session import VoiceSession, VoiceSessionManager, voice_session_manager

__all__ = ["VoiceSession", "VoiceSessionManager", "voice_session_manager"]
