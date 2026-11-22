// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 HobbyCoders.

// Robolectric 4.16.1 DefaultSdkProvider uses i7 SDKs. Include every @Config SDK
// plus compileSdk (resource loading can request it even in a lower-SDK test).
// SHA-256 values are pinned from Maven Central, not trusted from a restored cache.
val robolectricPins = mapOf(
    "android-all-instrumented:9-robolectric-4913185-2-i7" to "dbb1c88987b54cef3132695a1b4d2f4851ba06286a6fa7d15772f98fd1f0cdf7",
    "android-all-instrumented:14-robolectric-10818077-i7" to "3fdac2d463f431c6b8d83b9d0ca739e795de75fffedee648c6761bd4bd864701",
    "android-all-instrumented:15-robolectric-13954326-i7" to "06c4c8602d6db7486266a982bc55834ed6dfebd62a0b88daca204139ecf142cb",
    "android-all-instrumented:16-robolectric-13921718-i7" to "16f1f751643d1d3d5592008846bbdfc1e57cff15e6ec303d26584de3b6ac25ec",
    "nativeruntime-dist-compat:1.0.18" to "a6c34d92c990754e5aee076032a36f8e38fcacb5eb878ebe0a324509f3864eef",
)
// Separate configurations prevent Gradle's version conflict resolution from
// collapsing all four android-all-instrumented versions to the highest SDK.
val robolectricJars = robolectricPins.keys.associateWith { coordinate ->
    configurations.detachedConfiguration(dependencies.create("org.robolectric:$coordinate@jar")).apply {
        isTransitive = false
    }
}
// CI restores a dedicated actions/cache here (android.yml). A plain directory of
// jars, because Gradle ignores files-2.1 copies without its metadata index and
// asks Maven Central again. Unset (local builds): Gradle's own cache only.
val robolectricJarCache = providers.environmentVariable("ROBOLECTRIC_JAR_CACHE").orNull?.let(::File)
// This executes inside the Gradle daemon, after systemProp.* installation.
// Keep proof of the effective shared policy in debug and Play build logs.
val downloadAttempts = System.getProperty("org.gradle.internal.repository.max.tentatives")
val downloadBackoff = System.getProperty("org.gradle.internal.repository.initial.backoff")
logger.lifecycle("Gradle dependency-download policy (daemon): max.tentatives=$downloadAttempts; initial.backoff=$downloadBackoff ms")
val robolectricDependencyDir = layout.buildDirectory.dir("robolectric")
val prepareRobolectricDependencies = tasks.register("prepareRobolectricDependencies") {
    group = "verification"
    description = "Resolves and verifies pinned Robolectric jars before running offline unit tests."
    // Deliberately no up-to-date shortcut: recheck bytes even on cache hits.
    doLast {
        val cache = gradle.gradleUserHomeDir.resolve("caches/modules-2/files-2.1/org.robolectric")
        val jarCache = robolectricJarCache
        fun jarName(coordinate: String) = coordinate.replace(":", "-") + ".jar"
        fun sha256(jar: File): String {
            val digest = java.security.MessageDigest.getInstance("SHA-256")
            jar.inputStream().use { stream ->
                val buffer = ByteArray(1024 * 1024)
                var count = stream.read(buffer)
                while (count != -1) {
                    digest.update(buffer, 0, count)
                    count = stream.read(buffer)
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
        }
        val cachedCopies = robolectricPins.keys.associateWith { jarCache?.resolve(jarName(it))?.takeIf(File::isFile) }
        val present = robolectricPins.keys.count { coordinate ->
            val (artifact, version) = coordinate.split(":")
            cachedCopies[coordinate] != null ||
                cache.resolve("$artifact/$version").walkTopDown().any { it.name == "$artifact-$version.jar" }
        }
        val inJarCache = cachedCopies.values.count { it != null }
        logger.lifecycle("Robolectric cache before resolve: $present/${robolectricPins.size} pinned jars present " +
            "($inJarCache in the CI jar cache${jarCache?.let { " at $it" } ?: ", which is unset"})")
        // A restored copy is used only when its bytes match the pin; anything
        // else is discarded and resolved again, then checked like a download.
        val reused = mutableMapOf<String, File>()
        cachedCopies.forEach { (coordinate, jar) ->
            if (jar == null) return@forEach
            val expected = robolectricPins.getValue(coordinate)
            if (sha256(jar) == expected) {
                reused[coordinate] = jar
                logger.lifecycle("Verified Robolectric dependency: ${jar.name} SHA-256=$expected (CI jar cache)")
            } else {
                logger.warn("Discarded $jar: its SHA-256 does not match the pin; resolving it again")
                jar.delete()
            }
        }
        val resolved = robolectricJars.filterKeys { it !in reused }.values.flatMap { it.resolve() }.toSet()
        val files = reused.values.toSet() + resolved
        val expectedNames = robolectricPins.mapKeys { (coordinate, _) -> jarName(coordinate) }
        check(files.map { it.name }.toSet() == expectedNames.keys) { "Unexpected Robolectric artifacts: $files" }
        resolved.forEach { jar ->
            val expected = expectedNames.getValue(jar.name)
            val actual = sha256(jar)
            check(actual == expected) { "SHA-256 mismatch for $jar: expected $expected, got $actual" }
            logger.lifecycle("Verified Robolectric dependency: ${jar.name} SHA-256=$actual")
            // CI saves this directory only after the step succeeds (android.yml).
            if (jarCache != null) jar.copyTo(jarCache.resolve(jar.name), overwrite = true)
        }
        copy {
            from(files.filter { it.name.startsWith("android-all-instrumented-") })
            into(robolectricDependencyDir)
        }
    }
}
tasks.withType<Test>().configureEach {
    dependsOn(prepareRobolectricDependencies)
    systemProperty("robolectric.offline", "true")
    systemProperty("robolectric.dependency.dir", robolectricDependencyDir.get().asFile.absolutePath)
    // SHORT drops exception messages: FULL preserves HTTP status and URL from
    // HttpURLConnection through Robolectric's ExecutionException/AssertionError.
    testLogging {
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
        showExceptions = true
        showCauses = true
        showStackTraces = true
    }
    doFirst {
        logger.lifecycle("Robolectric offline=true; dependency.dir=${robolectricDependencyDir.get().asFile}")
    }
    // Gradle prints a count only on failure; keep the totals in passing CI logs too.
    val taskName = name
    afterSuite(KotlinClosure2<TestDescriptor, TestResult, Unit>({ suite, result ->
        if (suite.parent == null) logger.lifecycle(
            "$taskName: ${result.testCount} tests, ${result.successfulTestCount} passed, " +
                "${result.failedTestCount} failed, ${result.skippedTestCount} skipped")
    }))
}
