package com.hobbycoders.aishuffle.ui

import org.json.JSONObject

internal data class NativeVoiceRequest(val id: String, val sessionId: String) {
    companion object {
        fun parse(raw: String): NativeVoiceRequest {
            require(raw.length <= 2048)
            val json = JSONObject(raw)
            require(json.keys().asSequence().toSet() == setOf("type", "id", "sessionId"))
            require(json.getString("type") == "start")
            val id = json.getString("id")
            val session = json.getString("sessionId")
            require(id.matches(Regex("[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}")))
            require(session.matches(Regex("[a-zA-Z0-9_-]{1,128}")))
            return NativeVoiceRequest(id, session)
        }
    }
}
