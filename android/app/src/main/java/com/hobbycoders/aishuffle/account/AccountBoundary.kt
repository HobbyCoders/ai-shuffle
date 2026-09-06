package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server

/** Every account switch drops every account-owned key, including an earlier hub's keys. */
object AccountBoundary {
    fun directOnly(servers: List<Server>): List<Server> = servers.filter { it.accountId == null }
}
