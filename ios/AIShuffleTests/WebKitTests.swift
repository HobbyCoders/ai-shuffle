import XCTest
import WebKit
@testable import AIShuffle

/// Serves a fixed page on aishuffle://app and records every request it sees.
private final class RecordingHandler: NSObject, WKURLSchemeHandler {
    let page: String
    private(set) var requested: [String] = []

    init(page: String) { self.page = page }

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        let url = task.request.url!
        requested.append(url.absoluteString)
        let html = url.path.hasSuffix(".html")
        let body = html ? Data(page.utf8) : Data()
        task.didReceive(HTTPURLResponse(url: url, statusCode: html ? 200 : 404, httpVersion: "HTTP/1.1",
                                        headerFields: ["Content-Type": html ? "text/html" : "image/png"])!)
        if !body.isEmpty { task.didReceive(body) }
        task.didFinish()
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {}
}

/// Runs the real WebKit in the simulator: the bundled UI, the scheme handler and the content rules.
@MainActor
final class WebKitTests: XCTestCase {
    /// Kept alive for the whole test; WebKit throttles views that are not in a window.
    private var windows: [UIWindow] = []

    private func show(_ webView: WKWebView) {
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        let window = scene.map { UIWindow(windowScene: $0) } ?? UIWindow(frame: UIScreen.main.bounds)
        window.frame = UIScreen.main.bounds
        let controller = UIViewController()
        webView.frame = window.bounds
        controller.view.addSubview(webView)
        window.rootViewController = controller
        window.isHidden = false
        windows.append(window)
    }

    private func evaluate(_ webView: WKWebView, _ script: String) async throws -> String {
        try await withCheckedThrowingContinuation { continuation in
            webView.evaluateJavaScript("JSON.stringify((() => { \(script) })())") { value, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: value as? String ?? "null") }
            }
        }
    }

    private func waitUntil(_ timeout: TimeInterval, _ condition: () async throws -> Bool) async throws -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if try await condition() { return true }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        return try await condition()
    }

    private func compile(_ base: String) async throws -> WKContentRuleList {
        let server = Server(id: "t", name: "t", baseUrl: base, apiKey: "k", deviceId: "d")
        return try await ChatModel.compileRules(for: server)
    }

    func testBundledUIBootsInASecureContextWithItsUserAgent() async throws {
        let root = try XCTUnwrap(BundleAssets.root)
        XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent("index.html").path),
                      "The app bundle must contain mobile-ui/index.html")
        let rules = try await compile("https://worker.invalid")
        let webView = WKWebView(frame: .zero, configuration: ChatWebController.configuration(rules: rules, root: root))
        show(webView)
        webView.load(URLRequest(url: MobileUI.indexURL))

        let booted = try await waitUntil(30) {
            try await self.evaluate(webView, "return document.querySelector('body > div')?.childElementCount ?? 0") != "0"
        }
        XCTAssertTrue(booted, "The bundled SvelteKit app did not render")
        XCTAssertEqual(webView.url?.absoluteString, "aishuffle://app/assets/mobile-ui/")
        // getUserMedia and WebCrypto need a secure context; this is the design's open spike question.
        let facts = try await evaluate(webView, """
            return { secure: window.isSecureContext, subtle: typeof crypto.subtle, media: !!navigator.mediaDevices,
                     origin: location.origin, agent: navigator.userAgent }
            """)
        print("WebKit facts for aishuffle://app: \(facts)")
        let parsed = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(facts.utf8)) as? [String: Any])
        XCTAssertEqual(parsed["secure"] as? Bool, true)
        XCTAssertEqual(parsed["subtle"] as? String, "object")
        XCTAssertEqual(parsed["media"] as? Bool, true)
        XCTAssertEqual(parsed["origin"] as? String, MobileUI.origin)
        XCTAssertTrue((parsed["agent"] as? String ?? "").contains("AIShuffleIOS/"))
    }

    func testContentRulesBlockLoadsOutsideTheAllowlist() async throws {
        let page = """
            <html><body>
            <img src="aishuffle://app/allowed.png">
            <img src="aishuffle://other/blocked.png">
            <img src="aishuffle://app.evil/blocked.png">
            </body></html>
            """
        func load(rules: WKContentRuleList?) async throws -> [String] {
            let handler = RecordingHandler(page: page)
            let configuration = ChatWebController.configuration(rules: rules, root: URL(fileURLWithPath: "/"), schemeHandler: handler)
            let webView = WKWebView(frame: .zero, configuration: configuration)
            show(webView)
            webView.load(URLRequest(url: URL(string: "aishuffle://app/index.html")!))
            _ = try await waitUntil(10) { handler.requested.contains("aishuffle://app/allowed.png") }
            try await Task.sleep(nanoseconds: 1_500_000_000)
            return handler.requested
        }
        // Control: without the rules WebKit asks the handler for every image.
        let open = try await load(rules: nil)
        XCTAssertTrue(open.contains("aishuffle://other/blocked.png"), "\(open)")

        let filtered = try await load(rules: try await compile("https://worker.invalid"))
        XCTAssertTrue(filtered.contains("aishuffle://app/index.html"), "\(filtered)")
        XCTAssertTrue(filtered.contains("aishuffle://app/allowed.png"), "\(filtered)")
        XCTAssertFalse(filtered.contains("aishuffle://other/blocked.png"), "\(filtered)")
        XCTAssertFalse(filtered.contains("aishuffle://app.evil/blocked.png"), "\(filtered)")
    }

    /// Chat HTML previews are sandboxed srcdoc frames; the app's navigation delegate must let them render.
    func testChatControllerRendersSandboxedHtmlPreviewFrames() async throws {
        let root = try XCTUnwrap(BundleAssets.root)
        let server = Server(id: "t", name: "t", baseUrl: "https://worker.invalid", apiKey: "k", deviceId: "d")
        let controller = ChatWebController(server: server, rules: try await compile(server.baseUrl), root: root) { true }
        show(controller.webView)
        controller.loadIndex()
        let loaded = try await waitUntil(30) { !controller.webView.isLoading && MobileUI.isAssetOrigin(controller.webView.url) }
        XCTAssertTrue(loaded, "The chat controller did not load the bundled UI")
        _ = try await evaluate(controller.webView, """
            window.__previewMessages = [];
            addEventListener('message', (event) => window.__previewMessages.push(String(event.data)));
            const frame = document.createElement('iframe');
            frame.setAttribute('sandbox', 'allow-scripts');
            frame.srcdoc = '<script>parent.postMessage("preview-ran", "*")<' + '/script>';
            document.body.appendChild(frame);
            return true;
            """)
        let ran = try await waitUntil(10) {
            try await self.evaluate(controller.webView, "return window.__previewMessages.includes('preview-ran')") == "true"
        }
        XCTAssertTrue(ran, "A sandboxed srcdoc preview frame did not run")
    }

    private func loadedController() async throws -> ChatWebController {
        let root = try XCTUnwrap(BundleAssets.root)
        let server = Server(id: "t", name: "t", baseUrl: "https://worker.invalid", apiKey: "k", deviceId: "d")
        let controller = ChatWebController(server: server, rules: try await compile(server.baseUrl), root: root) { true }
        show(controller.webView)
        controller.loadIndex()
        let loaded = try await waitUntil(30) { !controller.webView.isLoading && MobileUI.isAssetOrigin(controller.webView.url) }
        XCTAssertTrue(loaded, "The chat controller did not load the bundled UI")
        return controller
    }

    /// `location` changes and `window.open` from the bundle are offered outside the app (Android
    /// treats them as gestures), and never navigate the chat document.
    func testScriptOpenedLinksAreOfferedOutsideTheApp() async throws {
        final class Offered { var urls: [URL] = [] }
        let offered = Offered()
        let controller = try await loadedController()
        controller.onExternalLink = { offered.urls.append($0) }

        _ = try await evaluate(controller.webView, "location.href = 'https://docs.example/script'; return true")
        let script = try await waitUntil(10) { offered.urls.contains(URL(string: "https://docs.example/script")!) }
        XCTAssertTrue(script, "A script location change was not offered: \(offered.urls)")

        // The same call the UI makes (frontend MainPanel.svelte). evaluateJavaScript counts as a
        // user gesture, so WebKit's popup gate lets it through as it would a tap; the content rules
        // must not block it first (they did, as a "popup" load).
        _ = try await evaluate(controller.webView, "window.open('https://docs.example/popup', '_blank', 'noopener'); return true")
        let popup = try await waitUntil(10) { offered.urls.contains(URL(string: "https://docs.example/popup")!) }
        XCTAssertTrue(popup, "window.open was not offered: \(offered.urls)")

        _ = try await evaluate(controller.webView, """
            const link = document.createElement('a');
            link.href = 'https://docs.example/blank'; link.target = '_blank'; link.rel = 'noopener';
            document.body.appendChild(link); link.click(); return true
            """)
        let blank = try await waitUntil(10) { offered.urls.contains(URL(string: "https://docs.example/blank")!) }
        XCTAssertTrue(blank, "A target=_blank link was not offered: \(offered.urls)")

        try await Task.sleep(nanoseconds: 500_000_000)
        XCTAssertTrue(MobileUI.isAssetOrigin(controller.webView.url), "\(String(describing: controller.webView.url))")
    }

    /// Closing must actually unload the chat document; about:blank is otherwise refused by the policy.
    func testCloseUnloadsTheChatDocument() async throws {
        let controller = try await loadedController()
        _ = try await evaluate(controller.webView, "window.__chatMarker = 'loaded'; return true")
        controller.close()
        let unloaded = try await waitUntil(10) {
            try await self.evaluate(controller.webView, "return location.href + '|' + String(window.__chatMarker)") ==
                "\"about:blank|undefined\""
        }
        XCTAssertTrue(unloaded, "close() left the chat document loaded")
    }

    func testSchemeHandlerRefusesWritesAndUnknownPaths() async throws {
        let root = try XCTUnwrap(BundleAssets.root)
        let webView = WKWebView(frame: .zero, configuration: ChatWebController.configuration(rules: nil, root: root))
        show(webView)
        webView.load(URLRequest(url: MobileUI.indexURL))
        _ = try await waitUntil(30) { !webView.isLoading && webView.url != nil }
        for (path, method, status) in [("assets/mobile-ui/version.json", "GET", "200"),
                                       ("assets/mobile-ui/version.json", "POST", "405"),
                                       ("assets/mobile-ui/missing.js", "GET", "403"),
                                       ("api/v1/files/a.png", "GET", "403")] {
            let result = try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Any?, Error>) in
                webView.callAsyncJavaScript("return (await fetch('/\(path)', {method: '\(method)'})).status + ''",
                                            arguments: [:], in: nil, in: .page) { result in
                    switch result {
                    case let .success(value): continuation.resume(returning: value)
                    case let .failure(error): continuation.resume(throwing: error)
                    }
                }
            }
            XCTAssertEqual(result as? String, status, "\(method) /\(path)")
        }
    }
}
