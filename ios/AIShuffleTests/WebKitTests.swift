import XCTest
import WebKit
@testable import AIShuffle

/// Records every load WebKit asks it for on the `fixture:` scheme, which the content rules must block.
private final class RecordingHandler: NSObject, WKURLSchemeHandler {
    private(set) var requested: [String] = []

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        let url = task.request.url!
        requested.append(url.absoluteString)
        task.didReceive(HTTPURLResponse(url: url, statusCode: 404, httpVersion: "HTTP/1.1", headerFields: [:])!)
        task.didFinish()
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {}
}

/// Runs the real WebKit in the simulator: the chat configuration, the bootstrap guard, the content
/// rules and the controller's navigation policy. There is no worker here, so the phone UI page is a
/// fixture loaded at the worker's `/mobile/` URL; the UI itself is tested in the frontend.
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

    private let worker = "https://worker.invalid"
    private let fixturePage = "<!doctype html><html><head><title>Phone UI</title></head><body><div id=app>ready</div></body></html>"

    private func loadFixture(_ webView: WKWebView, at url: String) async throws {
        webView.loadHTMLString(fixturePage, baseURL: URL(string: url)!)
        let loaded = try await waitUntil(10) {
            guard !webView.isLoading, webView.url?.absoluteString == url else { return false }
            return try await self.evaluate(webView, "return document.getElementById('app')?.textContent ?? ''") == "\"ready\""
        }
        XCTAssertTrue(loaded, "The fixture page did not load at \(url)")
    }

    func testThePhoneUiRunsInASecureContextWithItsUserAgent() async throws {
        let webView = WKWebView(frame: .zero, configuration: ChatWebController.configuration(rules: try await compile(worker)))
        show(webView)
        try await loadFixture(webView, at: try MobileUI.indexURL(base: worker).absoluteString)
        // getUserMedia and WebCrypto need a secure context; the worker's https origin is one.
        let facts = try await evaluate(webView, """
            return { secure: window.isSecureContext, subtle: typeof crypto.subtle, media: !!navigator.mediaDevices,
                     origin: location.origin, agent: navigator.userAgent }
            """)
        let parsed = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(facts.utf8)) as? [String: Any])
        XCTAssertEqual(parsed["secure"] as? Bool, true, facts)
        XCTAssertEqual(parsed["subtle"] as? String, "object", facts)
        XCTAssertEqual(parsed["media"] as? Bool, true, facts)
        XCTAssertEqual(parsed["origin"] as? String, worker)
        XCTAssertTrue((parsed["agent"] as? String ?? "").contains("AIShuffleIOS/"), facts)
    }

    /// WebKit user scripts cannot be scoped to an origin, so the bootstrap guards itself: the
    /// session and the capability descriptor reach the worker's top-level `/mobile/` page only.
    func testTheSessionReachesOnlyTheWorkersPhoneUi() async throws {
        let script = try MobileUI.bootstrapScript(apiOrigin: worker, token: "shc_test", version: "1.0", deviceName: "iPhone")
        func runtime(at url: String) async throws -> String {
            let configuration = ChatWebController.configuration(rules: nil)
            configuration.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart,
                                                                           forMainFrameOnly: true))
            let webView = WKWebView(frame: .zero, configuration: configuration)
            show(webView)
            try await loadFixture(webView, at: url)
            return try await evaluate(webView, """
                const value = window.__SHUFFLE_MOBILE__;
                return value ? [value.token, value.native.platform, value.native.bridge, value.native.capabilities.join(',')].join('|') : 'none'
                """)
        }
        let expected = "\"shc_test|ios|\(MobileUI.bridgeVersion)|\(MobileUI.capabilities.joined(separator: ","))\""
        for (url, want) in [("\(worker)/mobile/", expected), ("\(worker)/mobile/chat/abc", expected),
                            ("\(worker)/", "\"none\""), ("\(worker)/chat", "\"none\""),
                            ("https://other.invalid/mobile/", "\"none\"")] {
            let got = try await runtime(at: url)
            XCTAssertEqual(got, want, url)
        }
    }

    func testContentRulesBlockLoadsOutsideTheAllowlist() async throws {
        let pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
        let page = """
            <html><body>
            <img id=allowed src="\(pixel)">
            <img src="fixture://worker.invalid/blocked.png">
            <img src="fixture://other/blocked.png">
            </body></html>
            """
        func load(rules: WKContentRuleList?) async throws -> (requested: [String], dataLoaded: Bool) {
            let handler = RecordingHandler()
            let configuration = ChatWebController.configuration(rules: rules)
            configuration.setURLSchemeHandler(handler, forURLScheme: "fixture")
            let webView = WKWebView(frame: .zero, configuration: configuration)
            show(webView)
            // A data: document is on the allowlist, so the page loads with or without the rules.
            let encoded = page.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
            webView.load(URLRequest(url: URL(string: "data:text/html;charset=utf-8,\(encoded)")!))
            let dataLoaded = try await waitUntil(10) {
                try await self.evaluate(webView, "const i = document.getElementById('allowed'); return !!i && i.complete && i.naturalWidth > 0") == "true"
            }
            if rules == nil { _ = try await waitUntil(10) { handler.requested.count >= 2 } }
            try await Task.sleep(nanoseconds: 1_500_000_000)
            return (handler.requested, dataLoaded)
        }
        // Control: without the rules WebKit asks the handler for every image.
        let open = try await load(rules: nil)
        XCTAssertTrue(open.dataLoaded)
        XCTAssertTrue(open.requested.contains("fixture://other/blocked.png"), "\(open.requested)")
        XCTAssertTrue(open.requested.contains("fixture://worker.invalid/blocked.png"), "\(open.requested)")

        let filtered = try await load(rules: try await compile(worker))
        XCTAssertTrue(filtered.dataLoaded, "data: images are on the allowlist")
        XCTAssertEqual(filtered.requested, [], "Only the worker's https/wss origin, data: and blob: may load")
    }

    /// No worker answers here, so the controller must say the connection failed instead of a blank chat.
    func testAnUnreachableWorkerShowsAStatus() async throws {
        final class Statuses { var values: [String] = [] }
        let statuses = Statuses()
        let server = Server(id: "t", name: "Test worker", baseUrl: worker, apiKey: "k", deviceId: "d")
        let controller = ChatWebController(server: server, rules: try await compile(server.baseUrl)) { true }
        controller.onStatus = { statuses.values.append($0) }
        show(controller.webView)
        controller.loadIndex()
        let reported = try await waitUntil(30) { statuses.values.contains { $0.contains("could not reach Test worker") } }
        XCTAssertTrue(reported, "\(statuses.values)")
    }

    /// Chat HTML previews are sandboxed srcdoc frames; the app's navigation delegate must let them render.
    func testChatControllerRendersSandboxedHtmlPreviewFrames() async throws {
        let controller = try await loadedController()
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

    /// The chat controller with the fixture phone UI page at the worker's `/mobile/`, as `loadIndex()`
    /// would show it from a real worker. The load still passes the controller's navigation policy.
    private func loadedController() async throws -> ChatWebController {
        let server = Server(id: "t", name: "t", baseUrl: worker, apiKey: "k", deviceId: "d")
        let controller = ChatWebController(server: server, rules: try await compile(server.baseUrl)) { true }
        show(controller.webView)
        let index = try MobileUI.indexURL(base: worker)
        controller.webView.loadHTMLString(fixturePage, baseURL: index)
        let loaded = try await waitUntil(30) {
            !controller.webView.isLoading && MobileUI.isUiURL(base: self.worker, url: controller.webView.url)
        }
        XCTAssertTrue(loaded, "The chat controller did not load the phone UI page")
        return controller
    }

    /// `location` changes and `window.open` from the phone UI are offered outside the app (Android
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
        XCTAssertTrue(MobileUI.isUiURL(base: worker, url: controller.webView.url), "\(String(describing: controller.webView.url))")
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
}
