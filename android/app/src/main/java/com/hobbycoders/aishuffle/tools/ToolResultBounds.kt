package com.hobbycoders.aishuffle.tools

import org.json.JSONObject

/** The server bounds compact JSON with ensure_ascii=True, not just the UTF-8 wire body. */
internal object ToolResultBounds {
    // Leave room below the server's 64 KiB result and 65 KiB envelope limits.
    private const val MAX_BYTES = 60 * 1024
    private fun size(result: JSONObject): Int = result.toString().fold(0) { bytes, ch ->
        bytes + if (ch.code > 127) 6 else 1 // Supplementary code points have two UTF-16 surrogates.
    }
    fun fit(result: JSONObject): JSONObject {
        if (size(result) <= MAX_BYTES) return result
        // Keep a useful, explicitly partial screen snapshot for queued tool results.
        val nodes = result.optJSONArray("nodes")
        if (nodes != null && result.optBoolean("ok")) {
            result.put("truncated", true)
            while (nodes.length() > 0 && size(result) > MAX_BYTES) nodes.remove(nodes.length() - 1)
            if (size(result) <= MAX_BYTES) return result
        }
        return ToolRegistry.error("result_too_large",
            "Device output exceeded the delivery limit. The action may have run; verify device state before retrying.")
    }
}
