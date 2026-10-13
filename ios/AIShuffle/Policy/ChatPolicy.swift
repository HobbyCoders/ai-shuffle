import Foundation

/// What the chat WebView may load and which native capabilities its document may use.
/// Same rules as Android's ChatActivity, ChatAccessPolicy and ChatDownloadPolicy:
/// the document is only ever the paired worker's phone UI (`/mobile/`), and the network is
/// only that worker.
enum ChatPolicy {
    static let maxURLLength = 8192

    enum Navigation: Equatable {
        /// An in-app route of the phone UI.
        case allow
        case cancel
        /// Any other worker URL (a file, an API response, the desktop UI). It never navigates
        /// the chat document; Android saves it through the system file picker.
        case workerFile(URL)
        /// A user-tapped HTTPS link, opened outside the app after confirmation, with no credential.
        case openExternally(URL)
    }

    /// Worker URL for `url`: a URL on the worker's own origin. The page is on that origin, so a
    /// relative `/api/` link in old markdown already resolves here.
    static func workerURL(base: String, url: URL) -> URL? {
        let value = url.absoluteString
        return value.count <= maxURLLength && PairingInput.sameOrigin(base, value) ? url : nil
    }

    /// How WebKit says a navigation started.
    enum Trigger: Equatable {
        /// An `<a>` tap (`.linkActivated`), in place or with `target=_blank`.
        case linkTap
        /// A script `location` change (`.other`).
        case script
        /// `window.open`, which WebKit hands to `createWebViewWith`.
        case newWindow
        /// Back/forward, reload or a form submission.
        case other
    }

    /// Whether a navigation is the user's own: Android's `WebResourceRequest.hasGesture()`.
    /// WebKit has no public gesture flag. Link taps carry `.linkActivated`. `window.open` reaches
    /// `createWebViewWith` only from a user gesture, because `javaScriptCanOpenWindowsAutomatically`
    /// is off. A script `location` change is reported as `.other` with no gesture information,
    /// so it counts only when the phone UI's own main frame made it: HTML previews are
    /// sandboxed frames without top navigation or popups, and the user still confirms every
    /// external link before it opens.
    static func userInitiated(_ trigger: Trigger, fromUiMainFrame: Bool) -> Bool {
        switch trigger {
        case .linkTap: return true
        case .script, .newWindow: return fromUiMainFrame
        case .other: return false
        }
    }

    /// A WebKit security origin (port 0 for the scheme default) that is exactly the paired worker.
    static func isWorkerOrigin(base: String, scheme: String, host: String, port: Int) -> Bool {
        guard !scheme.isEmpty, !host.isEmpty, port >= 0 else { return false }
        let bracketed = host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
        return PairingInput.sameOrigin(base, "\(scheme)://\(bracketed)" + (port == 0 ? "" : ":\(port)") + "/")
    }

    static func navigation(base: String, url: URL?, mainFrame: Bool, userInitiated: Bool) -> Navigation {
        guard let url else { return .cancel }
        if MobileUI.isUiURL(base: base, url: url) { return .allow }
        if let worker = workerURL(base: base, url: url) { return .workerFile(worker) }
        // Chat HTML previews render in a sandboxed srcdoc frame (frontend HtmlPreview.svelte).
        // Android's shouldOverrideUrlLoading never sees that load; WebKit asks, so allow it for frames only.
        if !mainFrame, url.absoluteString == "about:srcdoc" { return .allow }
        if mainFrame, userInitiated, url.scheme?.lowercased() == "https", url.host != nil { return .openExternally(url) }
        return .cancel
    }

    /// Whether the document on screen is the worker's phone UI and the chat is still authorized.
    static func document(base: String, topLevel: URL?, authorized: Bool) -> Bool {
        authorized && MobileUI.isUiURL(base: base, url: topLevel)
    }

    /// The microphone goes only to the phone UI's own main frame, never to a frame or another document.
    static func microphone(base: String, topLevel: URL?, requestScheme: String, requestHost: String, requestPort: Int,
                           mainFrame: Bool, microphoneOnly: Bool, authorized: Bool) -> Bool {
        document(base: base, topLevel: topLevel, authorized: authorized) && mainFrame && microphoneOnly &&
            isWorkerOrigin(base: base, scheme: requestScheme, host: requestHost, port: requestPort)
    }

    /// WebKit content rules: block every load except the paired worker's HTTPS and
    /// WebSocket origin, and in-memory `data:`/`blob:` URLs. This is the iOS form of Android's
    /// `shouldInterceptRequest` allowlist; WKWebView cannot intercept https loads itself.
    static func contentRules(base: String) throws -> String {
        let origin = try PairingInput.origin(base)
        let hostAndPort = String(origin.dropFirst("https://".count))
        let allowed = [
            "^https://" + escape(hostAndPort) + "/",
            "^wss://" + escape(hostAndPort) + "/",
            "^data:",
            "^blob:",
        ]
        var rules: [[String: Any]] = [["trigger": ["url-filter": ".*"], "action": ["type": "block"]]]
        rules += allowed.map { filter -> [String: Any] in
            ["trigger": ["url-filter": filter], "action": ["type": "ignore-previous-rules"]]
        }
        // `window.open` and target=_blank links are "popup" loads, which the block-all rule would
        // stop before WebKit asks the UI delegate. Let HTTPS ones reach it: the delegate never
        // creates a window, it only offers the link outside the app after confirmation.
        rules.append(["trigger": ["url-filter": "^https://", "resource-type": ["popup"]],
                      "action": ["type": "ignore-previous-rules"]])
        let data = try JSONSerialization.data(withJSONObject: rules, options: [.sortedKeys, .withoutEscapingSlashes])
        return String(decoding: data, as: UTF8.self)
    }

    /// Hosts are already limited to `[a-z0-9.-]` or bracketed IPv6; escape the regex characters among them.
    private static func escape(_ value: String) -> String {
        value.map { ".[]".contains($0) ? "\\\($0)" : String($0) }.joined()
    }
}
