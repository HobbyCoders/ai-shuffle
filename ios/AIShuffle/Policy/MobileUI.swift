import Foundation

/// The app's own copy of the mobile UI.
///
/// `mobile/` at the repository root builds the UI; `ios/scripts/sync-mobile-ui.sh`
/// copies it to `ios/mobile-ui/`, which ships inside the app bundle. The chat
/// WebView serves it through `BundleSchemeHandler` at `indexURL`. A worker is
/// never asked for the UI; it only answers the page's HTTP/WebSocket calls,
/// authenticated with the paired session as a Bearer token that
/// `bootstrapScript` hands the page before any of its own script runs
/// (contract: frontend/src/lib/api/runtime.ts). Mirrors Android's MobileUi.kt.
enum MobileUI {
    static let scheme = "aishuffle"
    static let host = "app"
    /// The only origin that may load in the chat WebView.
    static let origin = "\(scheme)://\(host)"
    /// Directory name inside the app bundle.
    static let assetDirectory = "mobile-ui"
    /// The bundle's SvelteKit `paths.base`, the same path Android serves it at.
    static let path = "/assets/mobile-ui/"
    static let indexURL = URL(string: origin + path)!

    static func isAssetOrigin(_ url: URL?) -> Bool {
        guard let url, let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        return parts.scheme?.lowercased() == scheme && parts.host?.lowercased() == host &&
            parts.port == nil && parts.user == nil && parts.password == nil
    }

    /// The bundle root (with or without its trailing slash) is served as index.html.
    static func isIndexPath(_ value: String) -> Bool {
        value == path || value == String(path.dropLast())
    }

    /// Paired chat tokens are URL-safe base64; anything else is refused before it reaches script.
    static func isValidToken(_ token: String) -> Bool {
        !token.isEmpty && token.count <= 4096 && token.allSatisfy { tokenCharacters.contains($0) }
    }

    private static let tokenCharacters = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    /// Document-start script installing `window.__SHUFFLE_MOBILE__`, only on the bundle's own origin.
    static func bootstrapScript(apiOrigin: String, token: String, version: String?, deviceName: String?) throws -> String {
        guard isValidToken(token) else { throw ShuffleError("The worker returned an invalid chat authorization. Update the worker and retry.") }
        var config: [String: Any] = [
            "apiOrigin": try PairingInput.origin(apiOrigin),
            "token": token,
            "sendCookies": false,
        ]
        if let version = version?.trimmingCharacters(in: .whitespacesAndNewlines), !version.isEmpty { config["version"] = version }
        if let device = deviceName?.trimmingCharacters(in: .whitespacesAndNewlines), !device.isEmpty { config["deviceName"] = device }
        let json = try JSONSerialization.data(withJSONObject: config, options: [.sortedKeys, .withoutEscapingSlashes])
        return "if (location.protocol === '\(scheme):' && location.host === '\(host)') { " +
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
