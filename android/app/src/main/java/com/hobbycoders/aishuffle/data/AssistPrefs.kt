package com.hobbycoders.aishuffle.data

import android.content.Context
import android.content.SharedPreferences

/**
 * Non-sensitive settings for the voice assistant (plain SharedPreferences —
 * secrets stay in [ServerStore]'s encrypted store).
 */
class AssistPrefs private constructor(private val prefs: SharedPreferences) {

    /** Whether the always-on wake-word listener should run. */
    var wakeWordEnabled: Boolean
        get() = prefs.getBoolean(KEY_ENABLED, false)
        set(value) = prefs.edit().putBoolean(KEY_ENABLED, value).apply()

    /**
     * The spoken phrase that triggers the assistant. Normalized to lowercase
     * letters and spaces only — the phrase is embedded in the recognizer's
     * grammar (a JSON string), so punctuation could otherwise break it.
     */
    var wakePhrase: String
        get() = prefs.getString(KEY_PHRASE, DEFAULT_PHRASE) ?: DEFAULT_PHRASE
        set(value) {
            val clean = value.trim().lowercase()
                .replace(Regex("[^a-z ]"), "")
                .replace(Regex(" +"), " ")
                .trim()
                .ifBlank { DEFAULT_PHRASE }
            prefs.edit().putString(KEY_PHRASE, clean).apply()
        }

    /** Which saved server funds/hosts the voice session; null = first server. */
    var voiceServerId: String?
        get() = prefs.getString(KEY_SERVER_ID, null)
        set(value) {
            prefs.edit().apply {
                if (value == null) remove(KEY_SERVER_ID) else putString(KEY_SERVER_ID, value)
            }.apply()
        }

    /** Set once the Vosk model has been downloaded and unpacked. */
    var wakeModelReady: Boolean
        get() = prefs.getBoolean(KEY_MODEL_READY, false)
        set(value) = prefs.edit().putBoolean(KEY_MODEL_READY, value).apply()

    companion object {
        private const val PREFS_NAME = "ai_shuffle_assist"
        private const val KEY_ENABLED = "wake_word_enabled"
        private const val KEY_PHRASE = "wake_phrase"
        private const val KEY_SERVER_ID = "voice_server_id"
        private const val KEY_MODEL_READY = "wake_model_ready"

        const val DEFAULT_PHRASE = "hey shuffle"

        fun get(context: Context): AssistPrefs = AssistPrefs(
            context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE),
        )
    }
}
