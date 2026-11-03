package com.hobbycoders.aishuffle.tools

import java.io.IOException

/** HTTP failures retain their status; callers decide which operations can be retried. */
internal class ToolsHttpFailure(val statusCode: Int) : IOException("Device tools HTTP $statusCode")
