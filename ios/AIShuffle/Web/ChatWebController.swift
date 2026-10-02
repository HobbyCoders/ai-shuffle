import UIKit
import WebKit

/// The chat WebView: the app's own build of the mobile UI, which may load nothing but that
/// bundle and talk to nothing but the paired worker's HTTPS/WebSocket API. The worker never
/// serves a document here. Mirrors Android's ChatActivity:
///
/// - the bundle comes from `BundleSchemeHandler` (WebViewAssetLoader there);
/// - `ChatPolicy.contentRules` blocks every other load (shouldInterceptRequest there);
/// - navigation stays on the bundle; worker links never navigate the document;
/// - the paired session reaches the page only through a document-start script;
/// - there is no script message handler, so the page has no native bridge;
/// - the microphone is granted only to the bundle's own origin.
@MainActor
final class ChatWebController: NSObject, WKNavigationDelegate, WKUIDelegate {
    let webView: WKWebView
    private let server: Server
    private let authorized: () -> Bool
    var onStatus: (String) -> Void = { _ in }
    var onExternalLink: (URL) -> Void = { _ in }

    init(server: Server, rules: WKContentRuleList, root: URL, authorized: @escaping () -> Bool) {
        self.server = server
        self.authorized = authorized
        webView = WKWebView(frame: .zero, configuration: Self.configuration(rules: rules, root: root))
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsLinkPreview = false
        webView.allowsBackForwardNavigationGestures = false
    }

    /// Shared with the WebKit tests so they exercise the exact settings the app uses.
    static func configuration(rules: WKContentRuleList?, root: URL, schemeHandler: WKURLSchemeHandler? = nil) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        configuration.setURLSchemeHandler(schemeHandler ?? BundleSchemeHandler(root: root), forURLScheme: MobileUI.scheme)
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
        if MobileUI.isAssetOrigin(webView.url) { webView.evaluateJavaScript(update, completionHandler: nil) }
    }

    func loadIndex() {
        webView.load(URLRequest(url: MobileUI.indexURL))
    }

    func close() {
        webView.stopLoading()
        webView.configuration.userContentController.removeAllUserScripts()
        webView.loadHTMLString("", baseURL: nil)
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(handle(action) ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let allowed = !response.isForMainFrame || MobileUI.isAssetOrigin(response.response.url)
        decisionHandler(allowed && authorized() ? .allow : .cancel)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if authorized() { loadIndex() }
    }

    // MARK: WKUIDelegate

    /// No new windows: a target=_blank link gets the same treatment as a main-frame link.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        _ = handle(action)
        return nil
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        let allowed = ChatPolicy.microphone(topLevel: webView.url, requestScheme: origin.protocol, requestHost: origin.host,
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

    /// Applies ChatPolicy to a navigation; true only for in-app routes of the bundle.
    private func handle(_ action: WKNavigationAction) -> Bool {
        let mainFrame = action.targetFrame?.isMainFrame ?? true
        let decision = ChatPolicy.navigation(base: server.baseUrl, url: action.request.url, mainFrame: mainFrame,
                                             userInitiated: action.navigationType == .linkActivated)
        switch decision {
        case .allow:
            return authorized()
        case .cancel:
            return false
        case .workerFile:
            if mainFrame && action.navigationType == .linkActivated {
                onStatus("Saving worker files is not available in the iOS app yet.")
            }
            return false
        case let .openExternally(url):
            if ChatPolicy.document(topLevel: webView.url, authorized: authorized()) { onExternalLink(url) }
            return false
        }
    }

    /// Script dialogs only for the bundle's own main frame, presented over the chat.
    private func presenter(for frame: WKFrameInfo) -> UIViewController? {
        let origin = frame.securityOrigin
        guard frame.isMainFrame, ChatPolicy.document(topLevel: webView.url, authorized: authorized()),
              origin.protocol == MobileUI.scheme, origin.host == MobileUI.host,
              var top = webView.window?.rootViewController else { return nil }
        while let next = top.presentedViewController { top = next }
        return top
    }
}
