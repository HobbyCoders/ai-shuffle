package com.hobbycoders.aishuffle.wake

import android.content.Context
import com.hobbycoders.aishuffle.data.AssistPrefs
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.TimeUnit
import java.util.zip.ZipInputStream

/**
 * Downloads and unpacks the small offline Vosk speech model used for wake-word
 * spotting (~40 MB, one time, from the official Vosk model host). The model
 * lives in the app's private files dir; [modelDir] is what the recognizer
 * loads.
 */
object VoskModelManager {

    private const val MODEL_NAME = "vosk-model-small-en-us-0.15"
    private const val MODEL_URL = "https://alphacephei.com/vosk/models/$MODEL_NAME.zip"

    sealed class State {
        object Idle : State()
        data class Downloading(val percent: Int) : State()
        object Unpacking : State()
        object Ready : State()
        data class Error(val message: String) : State()
    }

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state

    fun modelDir(context: Context): File =
        File(File(context.filesDir, "wake-models"), MODEL_NAME)

    /** True when the model is unpacked on disk. */
    fun isReady(context: Context): Boolean {
        val ready = File(modelDir(context), "am").isDirectory
        if (ready) _state.value = State.Ready
        return ready
    }

    /**
     * Fetch and unpack the model if it isn't already present. Safe to call
     * repeatedly; progress is published on [state].
     */
    suspend fun ensureModel(context: Context): Boolean = withContext(Dispatchers.IO) {
        if (isReady(context)) return@withContext true
        if (_state.value is State.Downloading || _state.value == State.Unpacking) {
            return@withContext false // someone else is already on it
        }
        try {
            val client = OkHttpClient.Builder()
                .connectTimeout(30, TimeUnit.SECONDS)
                .readTimeout(120, TimeUnit.SECONDS)
                .build()
            val zipFile = File(context.cacheDir, "$MODEL_NAME.zip")

            _state.value = State.Downloading(0)
            client.newCall(Request.Builder().url(MODEL_URL).build()).execute().use { response ->
                if (!response.isSuccessful) {
                    throw IllegalStateException("Model download failed (HTTP ${response.code})")
                }
                val body = response.body ?: throw IllegalStateException("Empty model download")
                val total = body.contentLength()
                body.byteStream().use { input ->
                    FileOutputStream(zipFile).use { output ->
                        val buffer = ByteArray(64 * 1024)
                        var read: Int
                        var done = 0L
                        while (input.read(buffer).also { read = it } != -1) {
                            output.write(buffer, 0, read)
                            done += read
                            if (total > 0) {
                                _state.value = State.Downloading((done * 100 / total).toInt())
                            }
                        }
                    }
                }
            }

            _state.value = State.Unpacking
            val targetRoot = File(context.filesDir, "wake-models").apply { mkdirs() }
            ZipInputStream(zipFile.inputStream().buffered()).use { zip ->
                var entry = zip.nextEntry
                while (entry != null) {
                    val out = File(targetRoot, entry.name)
                    // Zip-slip guard: every entry must stay inside targetRoot.
                    if (!out.canonicalPath.startsWith(targetRoot.canonicalPath + File.separator)) {
                        throw SecurityException("Bad zip entry: ${entry.name}")
                    }
                    if (entry.isDirectory) {
                        out.mkdirs()
                    } else {
                        out.parentFile?.mkdirs()
                        FileOutputStream(out).use { zip.copyTo(it) }
                    }
                    zip.closeEntry()
                    entry = zip.nextEntry
                }
            }
            zipFile.delete()

            if (!File(modelDir(context), "am").isDirectory) {
                throw IllegalStateException("The model archive did not contain $MODEL_NAME")
            }
            AssistPrefs.get(context).wakeModelReady = true
            _state.value = State.Ready
            true
        } catch (e: Exception) {
            _state.value = State.Error(e.message ?: "Model download failed")
            false
        }
    }
}
