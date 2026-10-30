pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "AI Shuffle"
include(":app")
// The black-box test APK owns its runner dependencies. A regular androidTest
// APK deduplicates them against the app before R8 removes/renames app code.
if (providers.gradleProperty("minifiedSmoke").orNull == "true") {
    include(":minified-smoke")
}
