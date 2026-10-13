package com.hobbycoders.aishuffle.tools

import org.json.JSONArray
import org.json.JSONObject

object ToolRegistry {
    private val specs = linkedMapOf(
        "place_call" to listOf("number"), "send_sms" to listOf("number", "message"),
        "open_app" to listOf("package_name"), "open_url" to listOf("url"),
        "navigate_to" to listOf("destination"), "set_alarm" to listOf("hour", "minute", "label"),
        "set_timer" to listOf("seconds", "label"),
        "create_calendar_event" to listOf("title", "start_time", "end_time", "description", "location"),
        "create_reminder" to listOf("title", "start_time", "end_time", "description", "location"), "get_location" to emptyList(),
        "read_screen" to emptyList(), "tap_node" to listOf("ref"),
        "scroll" to listOf("ref", "direction"), "go_back" to listOf("ref"),
    )
    val names: Set<String> get() = specs.keys
    val queuedNames: Set<String> get() = names - setOf("place_call", "send_sms", "create_reminder")
    private val optional = setOf("label", "end_time", "description", "location")
    fun declarations(): JSONArray = JSONArray().apply {
        specs.forEach { (name, fields) ->
            val properties = JSONObject()
            fields.forEach { field -> properties.put(field, JSONObject().put("type", if (field in setOf("hour", "minute", "seconds")) "integer" else "string")) }
            if (name == "scroll") properties.getJSONObject("direction").put("enum", JSONArray(listOf("up", "down", "left", "right")))
            put(JSONObject().put("type", "function").put("name", name)
                .put("description", when (name) {
                    "place_call" -> "Open the system dialer; the user must place the call. Requires an explicit phone number."
                    "send_sms" -> "Open an SMS draft; only the user can send it. Requires an explicit phone number."
                    "create_calendar_event", "create_reminder" -> "Open the calendar event editor for the user to save; start_time/end_time are ISO8601 date-times with timezone."
                    "read_screen", "tap_node", "scroll", "go_back" -> "Optional accessibility action requiring local approval for EACH action. Use snapshot refs, never coordinates."
                    else -> "Android device action; requires enabled tools and foreground availability. Local approval may be required."
                }).put("parameters", JSONObject().put("type", "object").put("properties", properties).put("required", JSONArray(fields.filterNot { it in optional })).put("additionalProperties", false)))
        }
    }
    fun error(code: String, message: String): JSONObject = JSONObject().put("ok", false).put("status", code).put("error", message)
    fun text(args: JSONObject, key: String, optional: Boolean = false): String {
        val value = args.opt(key)
        if (optional && (value == null || value == JSONObject.NULL)) return ""
        require(value is String && value.isNotBlank() && value.length <= 4000) { "$key must be a non-empty string (max 4000 characters)." }
        return value
    }
    fun integer(args: JSONObject, key: String, range: IntRange): Int {
        val value = args.opt(key)
        require(value is Number && value.toDouble() == value.toInt().toDouble() && value.toInt() in range) { "$key must be an integer in $range." }
        return value.toInt()
    }
}
