import Foundation

/// The phone UI, served by the paired worker at `/mobile/`.
///
/// `mobile/` at the repository root builds it and every worker ships it beside the desktop UI
/// (app/mobile_ui.py), so a UI change reaches this app with a worker update and never needs an
/// App Store build. The app owns only the native side: pairing, permissions and the capabilities
/// in `capabilities`. The paired session reaches the page as a Bearer token through
/// `bootstrapScript`, installed before any page script runs (contract:
/// frontend/src/lib/api/runtime.ts). Mirrors Android's MobileUi.kt; see
/// docs/mobile-desktop-ui-split.md.
enum MobileUI {
    /// Path the worker serves the phone UI under (the build's `paths.base` plus `/`).
    static let path = "/mobile/"
    /// Version of the native bridge this app offers the page; same numbering as Android.
    static let bridgeVersion = 1
    /// What the page may rely on here. iOS has no save bridge yet: worker files are not saved.
    static let capabilities = ["file-chooser", "microphone"]

    /// The document the chat WebView loads from a worker.
    static func indexURL(base: String) throws -> URL {
        guard let url = URL(string: try PairingInput.origin(base) + path) else { throw ShuffleError("Invalid worker address.") }
        return url
    }

    /// Whether `url` is the phone UI on this worker: same origin, path `/mobile` or below and no
    /// dot segments. The only document the chat WebView may show, and the only one with capabilities.
    static func isUiURL(base: String, url: URL?) -> Bool {
        guard let url, PairingInput.sameOrigin(base, url.absoluteString),
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        let value = parts.percentEncodedPath
        guard value == String(path.dropLast()) || value.hasPrefix(path) else { return false }
        return !value.split(separator: "/").contains { $0 == ".." || $0.lowercased() == "%2e%2e" }
    }

    /// What to show when the worker answers the UI page with an HTTP error.
    static func loadFailure(status: Int) -> String {
        status == 404
            ? "This worker is too old to serve the phone UI. Update the worker, then refresh chat."
            : "The worker could not open chat (HTTP \(status)). Refresh chat to try again."
    }

    /// Paired chat tokens are URL-safe base64; anything else is refused before it reaches script.
    static func isValidToken(_ token: String) -> Bool {
        !token.isEmpty && token.count <= 4096 && token.allSatisfy { tokenCharacters.contains($0) }
    }

    private static let tokenCharacters = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    /// Document-start script installing `window.__SHUFFLE_MOBILE__`. WebKit user scripts cannot be
    /// scoped to an origin, so the guard keeps it to the worker's top-level phone UI page.
    static func bootstrapScript(apiOrigin: String, token: String, version: String?, deviceName: String?) throws -> String {
        guard isValidToken(token) else { throw ShuffleError("The worker returned an invalid chat authorization. Update the worker and retry.") }
        let origin = try PairingInput.origin(apiOrigin)
        var config: [String: Any] = [
            "apiOrigin": origin,
            "token": token,
            "sendCookies": false,
            "native": ["platform": "ios", "bridge": bridgeVersion, "capabilities": capabilities] as [String: Any],
        ]
        if let version = version?.trimmingCharacters(in: .whitespacesAndNewlines), !version.isEmpty { config["version"] = version }
        if let device = deviceName?.trimmingCharacters(in: .whitespacesAndNewlines), !device.isEmpty { config["deviceName"] = device }
        let json = try JSONSerialization.data(withJSONObject: config, options: [.sortedKeys, .withoutEscapingSlashes])
        let quotedOrigin = String(decoding: try JSONSerialization.data(withJSONObject: origin, options: [.fragmentsAllowed, .withoutEscapingSlashes]), as: UTF8.self)
        let quotedPath = String(decoding: try JSONSerialization.data(withJSONObject: path, options: [.fragmentsAllowed, .withoutEscapingSlashes]), as: UTF8.self)
        return "if (window.top === window && location.origin === \(quotedOrigin) && location.pathname.indexOf(\(quotedPath)) === 0) { " +
            "window.__SHUFFLE_MOBILE__ = \(String(decoding: json, as: UTF8.self)); }"
    }

    /// Hands an already-loaded page a fresh token without a reload.
    static func tokenUpdateScript(token: String) throws -> String {
        guard isValidToken(token) else { throw ShuffleError("The worker returned an invalid chat authorization. Update the worker and retry.") }
        let quoted = String(decoding: try JSONSerialization.data(withJSONObject: token, options: [.fragmentsAllowed]), as: UTF8.self)
        return "if (typeof window.shuffleMobileSetToken === 'function') { window.shuffleMobileSetToken(\(quoted)); } " +
            "else if (window.__SHUFFLE_MOBILE__) { window.__SHUFFLE_MOBILE__.token = \(quoted); }"
    }
}

/// Marks the chat WebView as the iOS app (`AIShuffleIOS/<version>`) for worker-side
/// diagnostics only. Appended to WebKit's application name so the real WebKit version stays visible.
enum WebUserAgent {
    static let product = "AIShuffleIOS"

    static func token(_ version: String?) -> String {
        let trimmed = version?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let value = trimmed.isEmpty ? "0" : trimmed.components(separatedBy: .whitespacesAndNewlines).filter { !$0.isEmpty }.joined(separator: "-")
        return "\(product)/\(value)"
    }

    static func decorate(_ base: String?, version: String?) -> String {
        let current = base?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if current.contains("\(product)/") { return current }
        return current.isEmpty ? token(version) : "\(current) \(token(version))"
    }
}
