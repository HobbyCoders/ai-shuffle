package com.hobbycoders.aishuffle.push

/** No URLs, tool inputs or question text are accepted from FCM. */
internal data class AttentionCommand(val worker: String, val session: String, val version: Long,
    val episode: String, val action: String, val body: String, val expiresAt: Long, val silent: Boolean) {
    companion object {
        private val identifier = Regex("[A-Za-z0-9_-]{1,128}")
        fun validId(value: String?) = value != null && identifier.matches(value)
        fun parse(data: Map<String, String>, now: Long): AttentionCommand? {
            if (data["type"] != "attention") return null
            val worker = data["worker_id"] ?: return null
            val session = data["session_id"] ?: return null
            val episode = data["episode"] ?: return null
            if (!validId(worker) || !validId(session) || (episode.isNotEmpty() && !validId(episode))) return null
            val version = data["version"]?.toLongOrNull()?.takeIf { it > 0 } ?: return null
            val expiry = data["expires_at"]?.toLongOrNull()?.takeIf { it > now && it <= now + 900 } ?: return null
            val action = data["action"]?.takeIf { it in setOf("alert", "update", "clear") } ?: return null
            val body = data["body"]?.takeIf { it.startsWith("A chat needs you: ") && it.length <= 160
                && it.none { char -> char.isISOControl() } } ?: "A chat needs you"
            return AttentionCommand(worker, session, version, episode, action, body, expiry, data["silent"] == "true")
        }
    }
}

internal data class AttentionReceipt(val version: Long = 0, val episode: String = "")
internal data class AttentionDecision(val receipt: AttentionReceipt, val show: Boolean, val alert: Boolean)

internal object AttentionPolicy {
    fun apply(old: AttentionReceipt, command: AttentionCommand): AttentionDecision? {
        if (command.version <= old.version) return null
        val alert = command.action != "clear" && !command.silent && command.episode.isNotEmpty() && command.episode != old.episode
        return AttentionDecision(AttentionReceipt(command.version, command.episode), command.action != "clear", alert)
    }
}
