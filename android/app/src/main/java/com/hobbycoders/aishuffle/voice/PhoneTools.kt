package com.hobbycoders.aishuffle.voice

import android.content.Context
import com.hobbycoders.aishuffle.tools.DeviceToolExecutor
import com.hobbycoders.aishuffle.tools.ToolRegistry
import org.json.JSONArray
import org.json.JSONObject

/** CHAT is deliberate continuous voice; COMMAND remains a backend wire compatibility value. */
enum class SessionMode { COMMAND, CHAT }

data class ToolOutcome(val result: JSONObject, val endSession: Boolean = false, val endDelayMs: Long = 2500)

/** Realtime adapter over the same Android executor used by authenticated queued commands. */
class PhoneTools(context: Context) {
    private val executor = DeviceToolExecutor(context)
    fun declarations(): JSONArray = ToolRegistry.declarations().apply {
        put(JSONObject().put("type", "function").put("name", "end_session")
            .put("description", "End the voice conversation only when the user asks to end it; CHAT stays connected during silence.")
            .put("parameters", JSONObject().put("type", "object").put("properties", JSONObject())))
    }
    fun owns(name: String): Boolean = name == "end_session" || name in ToolRegistry.names
    suspend fun execute(name: String, args: JSONObject): ToolOutcome =
        if (name == "end_session") ToolOutcome(JSONObject().put("ok", true).put("message", "Session ending."), endSession = true)
        else try { ToolOutcome(executor.execute(name, args)) }
        catch (e: kotlinx.coroutines.CancellationException) { throw e }
        catch (_: Exception) { ToolOutcome(ToolRegistry.error("unavailable", "Phone tools could not access local storage or Android services.")) }
}
