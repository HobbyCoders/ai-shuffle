package com.hobbycoders.aishuffle.ui

import android.content.Context
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import com.hobbycoders.aishuffle.net.WebSessionReservation
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

/** Capture worker authorization and the account's credential epoch, without logging either. */
internal class ChatSessionOwner(context: Context, private val server: Server) {
    private val accounts = AccountStore(context)
    private val workers = ServerStore.get(context)
    private val account = accounts.session()

    fun current(): Boolean = sameAuthorization(server, workers.getById(server.id)) &&
        (account?.let(accounts::matches) ?: !accounts.hasStoredSession()) &&
        (server.accountId == null || account?.let {
            it.accountId == server.accountId && it.hub == server.hubUrl && it.deviceId == server.deviceId
        } == true)

    companion object {
        // A name or push-endpoint refresh does not change who may use this chat.
        internal fun sameAuthorization(expected: Server, saved: Server?): Boolean =
            com.hobbycoders.aishuffle.net.DeviceKeyRenewal.sameAuthorization(expected, saved)
    }
}

/** Process-local fences also cover replacement ChatActivities sharing Android's cookie jar. */
internal object WebSessionBootstrap {
    class Attempt internal constructor(internal val origin: String)
    private val attempts = mutableMapOf<String, Attempt>()
    // Fixed stripes avoid retaining worker URLs or credentials after activities close.
    private val legacyLocks = Array(16) { Mutex() }
    private val cookieLocks = Array(16) { Mutex() }
    private fun lock(locks: Array<Mutex>, origin: String) = locks[(origin.hashCode() and Int.MAX_VALUE) % locks.size]

    @Synchronized fun begin(origin: String) = Attempt(origin).also { attempts[origin] = it }
    @Synchronized fun current(attempt: Attempt) = attempts[attempt.origin] === attempt
    @Synchronized fun cancel(attempt: Attempt?) {
        if (attempt != null && attempts[attempt.origin] === attempt) attempts.remove(attempt.origin)
    }

    private suspend fun check(attempt: Attempt, owns: () -> Boolean) {
        currentCoroutineContext().ensureActive()
        if (!current(attempt) || !owns()) throw CancellationException("The chat connection changed.")
    }

    suspend fun mint(server: Server, attempt: Attempt, owns: () -> Boolean): ApiResult<String> {
        repeat(2) { retry ->
            check(attempt, owns)
            when (val reserved = ApiClient.reserveWebSession(server)) {
                is ApiResult.Success -> {
                    check(attempt, owns)
                    when (val reservation = reserved.data) {
                        WebSessionReservation.Unsupported -> {
                            if (retry != 0) return ApiResult.Failure("The worker changed its session protocol. Update the worker and retry.")
                            // A canceled blocking legacy mint still owns this lock until its call
                            // returns. Old workers cannot guarantee ordering after an ambiguous timeout.
                            return lock(legacyLocks, server.baseUrl).withLock {
                                check(attempt, owns)
                                val result = withContext(NonCancellable) { ApiClient.createWebSession(server) }
                                check(attempt, owns)
                                result
                            }
                        }
                        is WebSessionReservation.Ready -> {
                            if (ApiClient.monotonicMillis() >= reservation.deadlineMillis)
                                return ApiResult.Failure("The connection request expired. Refresh chat to try again.")
                            val result = ApiClient.commitWebSession(server, reservation)
                            check(attempt, owns)
                            if (result is ApiResult.Failure && result.statusCode == 409) {
                                if (retry == 1) return ApiResult.Failure("Another connection replaced this request. Refresh chat to try again.", 409)
                            } else return result
                        }
                    }
                }
                is ApiResult.Failure -> { check(attempt, owns); return reserved }
                ApiResult.NeedsCfAccess -> { check(attempt, owns); return ApiResult.NeedsCfAccess }
            }
        }
        error("Unreachable retry state")
    }

    /** CookieManager writes cannot be canceled. Drain an accepted write before a newer one. */
    suspend fun installCookie(attempt: Attempt, owns: () -> Boolean, write: ((Boolean) -> Unit) -> Unit): Boolean =
        lock(cookieLocks, attempt.origin).withLock {
            check(attempt, owns)
            val accepted = withContext(NonCancellable) {
                suspendCoroutine { continuation -> write { continuation.resume(it) } }
            }
            check(attempt, owns)
            accepted
        }
}
