import UIKit
import WebKit

/// The chat WebView: the paired worker's phone UI at `/mobile/`, which may talk to nothing but
/// that worker's HTTPS/WebSocket API. UI changes ship with the worker, not the app. Mirrors
/// Android's ChatActivity:
///
/// - the only document is `MobileUI.indexURL` and its in-app routes; another worker page
///   (the desktop UI, an API response) is refused, and an HTTP error shows a status instead;
/// - `ChatPolicy.contentRules` blocks every other load (shouldInterceptRequest there);
/// - worker links never navigate the document;
/// - the paired session reaches the page only through a guarded document-start script;
/// - there is no script message handler, so the page has no native bridge;
/// - the microphone is granted only to the phone UI's own main frame.
@MainActor
final class ChatWebController: NSObject, WKNavigationDelegate, WKUIDelegate {
    let webView: WKWebView
    private let server: Server
    private let authorized: () -> Bool
    /// Set by `close()`: from then on the only navigation allowed is the blank page that unloads the chat.
    private var closing = false
    var onStatus: (String) -> Void = { _ in }
    var onExternalLink: (URL) -> Void = { _ in }

    init(server: Server, rules: WKContentRuleList, authorized: @escaping () -> Bool) {
        self.server = server
        self.authorized = authorized
        webView = WKWebView(frame: .zero, configuration: Self.configuration(rules: rules))
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsLinkPreview = false
        webView.allowsBackForwardNavigationGestures = false
    }

    /// Shared with the WebKit tests so they exercise the exact settings the app uses.
    static func configuration(rules: WKContentRuleList?) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        if let rules { configuration.userContentController.add(rules) }
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        configuration.allowsInlineMediaPlayback = true
        configuration.dataDetectorTypes = []
        configuration.applicationNameForUserAgent = WebUserAgent.decorate(configuration.applicationNameForUserAgent, version: appVersion)
        return configuration
    }

    static var appVersion: String? { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String }

    /// Install the page's runtime (worker origin + Bearer token) before any page script runs,
    /// and hand an already-loaded page the new token without a reload.
    func install(token: String) throws {
        let script = try MobileUI.bootstrapScript(apiOrigin: server.baseUrl, token: token, version: Self.appVersion,
                                                  deviceName: UIDevice.current.model)
        let update = try MobileUI.tokenUpdateScript(token: token)
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        controller.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        if MobileUI.isUiURL(base: server.baseUrl, url: webView.url) { webView.evaluateJavaScript(update, completionHandler: nil) }
    }

    func loadIndex() {
        guard let url = try? MobileUI.indexURL(base: server.baseUrl) else {
            onStatus("This worker address is invalid. Remove it and pair again.")
            return
        }
        webView.load(URLRequest(url: url))
    }

    /// Unloads the chat document (and the token in its memory). The blank page is about:blank,
    /// which the navigation policy refuses, so `closing` admits exactly that one load.
    func close() {
        closing = true
        webView.stopLoading()
        webView.configuration.userContentController.removeAllUserScripts()
        webView.loadHTMLString("", baseURL: nil)
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if closing {
            let blank = (action.targetFrame?.isMainFrame ?? false) && action.request.url?.absoluteString == "about:blank"
            decisionHandler(blank ? .allow : .cancel)
            return
        }
        decisionHandler(handle(action, newWindow: false) ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if closing {
            decisionHandler(response.isForMainFrame && response.response.url?.absoluteString == "about:blank" ? .allow : .cancel)
            return
        }
        if response.isForMainFrame, let http = response.response as? HTTPURLResponse, http.statusCode >= 400,
           MobileUI.isUiURL(base: server.baseUrl, url: http.url) {
            // Usually a worker from before /mobile/ existed (404): it must be updated, not the app.
            onStatus(MobileUI.loadFailure(status: http.statusCode))
            decisionHandler(.cancel)
            return
        }
        let allowed = !response.isForMainFrame || MobileUI.isUiURL(base: server.baseUrl, url: response.response.url)
        decisionHandler(allowed && authorized() ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let failure = error as NSError
        // Cancelled loads (including the ones this controller refuses) are not connection failures.
        guard !closing, failure.domain == NSURLErrorDomain, failure.code != NSURLErrorCancelled else { return }
        onStatus("Chat could not reach \(server.name). Check the connection, then refresh chat.")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if !closing && authorized() { loadIndex() }
    }

    // MARK: WKUIDelegate

    /// No new windows: a target=_blank link or a `window.open` gets the same treatment as a main-frame link.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if !closing { _ = handle(action, newWindow: true) }
        return nil
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        let allowed = ChatPolicy.microphone(base: server.baseUrl, topLevel: webView.url, requestScheme: origin.protocol, requestHost: origin.host,
                                            requestPort: origin.port, mainFrame: frame.isMainFrame,
                                            microphoneOnly: type == .microphone,
                                            authorized: authorized() && UIApplication.shared.applicationState == .active)
        decisionHandler(allowed ? .grant : .deny)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        guard let presenter = presenter(for: frame) else { completionHandler(); return }
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        presenter.present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard let presenter = presenter(for: frame) else { completionHandler(false); return }
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        presenter.present(alert, animated: true)
    }

    // MARK: Helpers

    /// Applies ChatPolicy to a navigation; true only for in-app routes of the phone UI.
    /// `newWindow` is true for `createWebViewWith`, whose target is a window that will never exist.
    private func handle(_ action: WKNavigationAction, newWindow: Bool) -> Bool {
        let mainFrame = action.targetFrame?.isMainFrame ?? true
        let trigger: ChatPolicy.Trigger
        switch action.navigationType {
        case .linkActivated: trigger = .linkTap
        case .other: trigger = newWindow ? .newWindow : .script
        default: trigger = .other
        }
        let userInitiated = ChatPolicy.userInitiated(trigger, fromUiMainFrame: isUiMainFrame(Self.sourceFrame(of: action)))
        let decision = ChatPolicy.navigation(base: server.baseUrl, url: action.request.url, mainFrame: mainFrame,
                                             userInitiated: userInitiated)
        switch decision {
        case .allow:
            return authorized()
        case .cancel:
            return false
        case .workerFile:
            if mainFrame && userInitiated {
                onStatus("Saving worker files is not available in the iOS app yet.")
            }
            return false
        case let .openExternally(url):
            if ChatPolicy.document(base: server.baseUrl, topLevel: webView.url, authorized: authorized()) { onExternalLink(url) }
            return false
        }
    }

    /// `sourceFrame` is declared non-optional, but WebKit can leave it nil (for example for
    /// loads the app itself starts); read it without trusting the annotation.
    private static func sourceFrame(of action: WKNavigationAction) -> WKFrameInfo? {
        action.value(forKey: "sourceFrame") as? WKFrameInfo
    }

    /// The phone UI's own main frame, while it is the document on screen and the chat is authorized.
    private func isUiMainFrame(_ frame: WKFrameInfo?) -> Bool {
        guard let frame, frame.isMainFrame else { return false }
        let origin = frame.securityOrigin
        return ChatPolicy.document(base: server.baseUrl, topLevel: webView.url, authorized: authorized()) &&
            ChatPolicy.isWorkerOrigin(base: server.baseUrl, scheme: origin.protocol, host: origin.host, port: origin.port)
    }

    /// Script dialogs only for the phone UI's own main frame, presented over the chat.
    private func presenter(for frame: WKFrameInfo) -> UIViewController? {
        guard isUiMainFrame(frame), var top = webView.window?.rootViewController else { return nil }
        while let next = top.presentedViewController { top = next }
        return top
    }
}
