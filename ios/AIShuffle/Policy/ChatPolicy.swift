import Foundation

/// What the chat WebView may load and which native capabilities its document may use.
/// Same rules as Android's ChatActivity, ChatAccessPolicy and ChatDownloadPolicy:
/// the document is only ever the bundled UI, and the network is only the paired worker.
enum ChatPolicy {
    static let maxURLLength = 8192

    enum Navigation: Equatable {
        /// An in-app route of the bundled UI.
        case allow
        case cancel
        /// A worker link (or an old `/api` link resolved against the bundle). It never
        /// navigates the chat document; Android saves it through the system file picker.
        case workerFile(URL)
        /// A user-tapped HTTPS link, opened outside the app after confirmation, with no credential.
        case openExternally(URL)
    }

    /// Worker URL for `url`: a URL on the worker's own origin, or a bundle-origin `/api/` path
    /// that old markdown stores and the browser resolves against the bundle.
    static func workerURL(base: String, url: URL) -> URL? {
        let value = url.absoluteString
        if value.count <= maxURLLength, PairingInput.sameOrigin(base, value) { return url }
        guard MobileUI.isAssetOrigin(url), let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.percentEncodedPath.hasPrefix("/api/"), let origin = try? PairingInput.origin(base) else { return nil }
        let resolved = origin + parts.percentEncodedPath + (parts.percentEncodedQuery.map { "?\($0)" } ?? "")
        guard resolved.count <= maxURLLength, PairingInput.sameOrigin(base, resolved) else { return nil }
        return URL(string: resolved)
    }

    static func navigation(base: String, url: URL?, mainFrame: Bool, userInitiated: Bool) -> Navigation {
        guard let url else { return .cancel }
        if let worker = workerURL(base: base, url: url) { return .workerFile(worker) }
        if MobileUI.isAssetOrigin(url) { return .allow }
        // Chat HTML previews render in a sandboxed srcdoc frame (frontend HtmlPreview.svelte).
        // Android's shouldOverrideUrlLoading never sees that load; WebKit asks, so allow it for frames only.
        if !mainFrame, url.absoluteString == "about:srcdoc" { return .allow }
        if mainFrame, userInitiated, url.scheme?.lowercased() == "https", url.host != nil { return .openExternally(url) }
        return .cancel
    }

    /// Whether the document on screen is the bundled UI and the chat is still authorized.
    static func document(topLevel: URL?, authorized: Bool) -> Bool {
        authorized && MobileUI.isAssetOrigin(topLevel)
    }

    /// The microphone goes only to the bundled UI's own origin, never to a frame or another document.
    static func microphone(topLevel: URL?, requestScheme: String, requestHost: String, requestPort: Int,
                           mainFrame: Bool, microphoneOnly: Bool, authorized: Bool) -> Bool {
        document(topLevel: topLevel, authorized: authorized) && mainFrame && microphoneOnly &&
            requestScheme.lowercased() == MobileUI.scheme && requestHost.lowercased() == MobileUI.host && requestPort == 0
    }

    /// WebKit content rules: block every load except the bundle, the paired worker's HTTPS and
    /// WebSocket origin, and in-memory `data:`/`blob:` URLs. This is the iOS form of Android's
    /// `shouldInterceptRequest` allowlist; WKWebView cannot intercept https loads itself.
    static func contentRules(base: String) throws -> String {
        let origin = try PairingInput.origin(base)
        let hostAndPort = String(origin.dropFirst("https://".count))
        let allowed = [
            "^" + escape(MobileUI.origin) + "/",
            "^https://" + escape(hostAndPort) + "/",
            "^wss://" + escape(hostAndPort) + "/",
            "^data:",
            "^blob:",
        ]
        var rules: [[String: Any]] = [["trigger": ["url-filter": ".*"], "action": ["type": "block"]]]
        rules += allowed.map { filter -> [String: Any] in
            ["trigger": ["url-filter": filter], "action": ["type": "ignore-previous-rules"]]
        }
        let data = try JSONSerialization.data(withJSONObject: rules, options: [.sortedKeys, .withoutEscapingSlashes])
        return String(decoding: data, as: UTF8.self)
    }

    /// Hosts are already limited to `[a-z0-9.-]` or bracketed IPv6; escape the regex characters among them.
    private static func escape(_ value: String) -> String {
        value.map { ".[]".contains($0) ? "\\\($0)" : String($0) }.joined()
    }
}
