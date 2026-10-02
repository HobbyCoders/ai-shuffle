import XCTest
@testable import AIShuffle

final class PairingInputTests: XCTestCase {
    func testOriginIsNormalized() throws {
        XCTAssertEqual(try PairingInput.origin(" HTTPS://Worker.Example:443/ "), "https://worker.example")
        XCTAssertEqual(try PairingInput.origin("https://worker.example:8443"), "https://worker.example:8443")
        XCTAssertEqual(try PairingInput.origin("https://[::1]:8443"), "https://[::1]:8443")
    }

    func testOriginRejectsAnythingButAnHTTPSOrigin() {
        for raw in ["http://worker.example", "worker.example", "https://", "https://worker.example/path",
                    "https://worker.example/?q=1", "https://worker.example/#x", "https://user:pw@worker.example",
                    "https://worker.example:0", "https://bad_host.example", "https://exa mple.com",
                    "https://worker..example", "ftp://worker.example", "javascript:alert(1)"] {
            XCTAssertThrowsError(try PairingInput.origin(raw), raw)
        }
    }

    func testCode() throws {
        XCTAssertEqual(try PairingInput.code("abcd-efgh 1234"), "ABCDEFGH1234")
        XCTAssertThrowsError(try PairingInput.code("ABCDEFGH123"))
        XCTAssertThrowsError(try PairingInput.code("ABCDEFGH123!"))
    }

    func testPairingLink() throws {
        // The link a worker actually shows (app/core/pairing.py build_pairing_url).
        XCTAssertEqual(try PairingInput.parse("https://Worker.Example/pair#token=abcd-efgh-1234"),
                       PairingInput.Pair(origin: "https://worker.example", code: "ABCDEFGH1234"))
        XCTAssertEqual(try PairingInput.parse("https://worker.example:8443/pair#token=ABCD-EFGH-1234"),
                       PairingInput.Pair(origin: "https://worker.example:8443", code: "ABCDEFGH1234"))
        for raw in ["https://worker.example/pair?x=1#token=ABCDEFGH1234", "https://worker.example/other#token=ABCDEFGH1234",
                    "https://worker.example/pair#token=ABCDEFGH1234&code=x", "http://worker.example/pair#token=ABCDEFGH1234",
                    "https://user@worker.example/pair#token=ABCDEFGH1234", "https://worker.example/pair#code=ABCDEFGH1234"] {
            XCTAssertThrowsError(try PairingInput.parse(raw), raw)
        }
    }

    func testSameOrigin() {
        let base = "https://worker.example"
        XCTAssertTrue(PairingInput.sameOrigin(base, "https://worker.example/api/v1/x?y=1"))
        XCTAssertTrue(PairingInput.sameOrigin(base, "https://WORKER.example:443/"))
        XCTAssertFalse(PairingInput.sameOrigin(base, "https://worker.example:8443/"))
        XCTAssertFalse(PairingInput.sameOrigin(base, "http://worker.example/"))
        XCTAssertFalse(PairingInput.sameOrigin(base, "https://user@worker.example/"))
        XCTAssertFalse(PairingInput.sameOrigin(base, "https://worker.example.evil.test/"))
        XCTAssertFalse(PairingInput.sameOrigin(base, "aishuffle://app/assets/mobile-ui/"))
    }
}

final class MobileUITests: XCTestCase {
    private func config(_ script: String) throws -> [String: Any] {
        let start = try XCTUnwrap(script.range(of: "window.__SHUFFLE_MOBILE__ = "))
        let end = try XCTUnwrap(script.range(of: "; }", options: .backwards))
        let json = String(script[start.upperBound..<end.lowerBound])
        return try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
    }

    func testBootstrapScriptIsGuardedToTheBundleOrigin() throws {
        let script = try MobileUI.bootstrapScript(apiOrigin: "https://Worker.Example:443", token: "shc_abc-DEF_123",
                                                  version: " 0.9.0 ", deviceName: "iPhone")
        XCTAssertTrue(script.hasPrefix("if (location.protocol === 'aishuffle:' && location.host === 'app') { "))
        let values = try config(script)
        XCTAssertEqual(values["apiOrigin"] as? String, "https://worker.example")
        XCTAssertEqual(values["token"] as? String, "shc_abc-DEF_123")
        XCTAssertEqual(values["sendCookies"] as? Bool, false)
        XCTAssertEqual(values["version"] as? String, "0.9.0")
        XCTAssertEqual(values["deviceName"] as? String, "iPhone")
        XCTAssertNil(try config(MobileUI.bootstrapScript(apiOrigin: "https://w.example", token: "t", version: " ", deviceName: nil))["version"])
    }

    func testScriptsRefuseUnsafeTokensAndOrigins() {
        for token in ["", "a b", "a'b", "a\"b", "a</script>", String(repeating: "a", count: 4097)] {
            XCTAssertThrowsError(try MobileUI.bootstrapScript(apiOrigin: "https://w.example", token: token, version: nil, deviceName: nil), token)
            XCTAssertThrowsError(try MobileUI.tokenUpdateScript(token: token), token)
        }
        XCTAssertThrowsError(try MobileUI.bootstrapScript(apiOrigin: "http://w.example", token: "t", version: nil, deviceName: nil))
    }

    func testTokenUpdateScript() throws {
        let script = try MobileUI.tokenUpdateScript(token: "shc_new")
        XCTAssertTrue(script.contains(#"window.shuffleMobileSetToken("shc_new")"#))
        XCTAssertTrue(script.contains(#"window.__SHUFFLE_MOBILE__.token = "shc_new""#))
    }

    func testBundleOriginAndIndexPath() {
        XCTAssertEqual(MobileUI.indexURL.absoluteString, "aishuffle://app/assets/mobile-ui/")
        XCTAssertTrue(MobileUI.isAssetOrigin(URL(string: "aishuffle://app/assets/mobile-ui/_app/x.js")))
        XCTAssertTrue(MobileUI.isAssetOrigin(URL(string: "AISHUFFLE://APP/")))
        XCTAssertFalse(MobileUI.isAssetOrigin(URL(string: "aishuffle://app:1/")))
        XCTAssertFalse(MobileUI.isAssetOrigin(URL(string: "aishuffle://other/")))
        XCTAssertFalse(MobileUI.isAssetOrigin(URL(string: "https://app/")))
        XCTAssertTrue(MobileUI.isIndexPath("/assets/mobile-ui/"))
        XCTAssertTrue(MobileUI.isIndexPath("/assets/mobile-ui"))
        XCTAssertFalse(MobileUI.isIndexPath("/assets/mobile-ui/index"))
    }

    func testUserAgentToken() {
        XCTAssertEqual(WebUserAgent.decorate("Mobile/15E148", version: "0.9 beta"), "Mobile/15E148 AIShuffleIOS/0.9-beta")
        XCTAssertEqual(WebUserAgent.decorate(nil, version: nil), "AIShuffleIOS/0")
        XCTAssertEqual(WebUserAgent.decorate("Mobile AIShuffleIOS/1", version: "2"), "Mobile AIShuffleIOS/1")
    }
}

final class ChatPolicyTests: XCTestCase {
    private let base = "https://worker.example"

    func testNavigation() {
        func decide(_ raw: String?, main: Bool = true, tap: Bool = true) -> ChatPolicy.Navigation {
            ChatPolicy.navigation(base: base, url: raw.flatMap { URL(string: $0) }, mainFrame: main, userInitiated: tap)
        }
        XCTAssertEqual(decide("aishuffle://app/assets/mobile-ui/", tap: false), .allow)
        XCTAssertEqual(decide("aishuffle://app/assets/mobile-ui/sessions/1"), .allow)
        XCTAssertEqual(decide("https://worker.example/api/v1/files/a.png"),
                       .workerFile(URL(string: "https://worker.example/api/v1/files/a.png")!))
        // Old markdown /api links resolve against the bundle origin; they mean the worker.
        XCTAssertEqual(decide("aishuffle://app/api/v1/files/a.png?x=1"),
                       .workerFile(URL(string: "https://worker.example/api/v1/files/a.png?x=1")!))
        XCTAssertEqual(decide("https://docs.example/page"), .openExternally(URL(string: "https://docs.example/page")!))
        XCTAssertEqual(decide("https://docs.example/page", tap: false), .cancel)
        XCTAssertEqual(decide("https://docs.example/page", main: false), .cancel)
        XCTAssertEqual(decide("http://docs.example/page"), .cancel)
        XCTAssertEqual(decide("javascript:alert(1)"), .cancel)
        XCTAssertEqual(decide("aishuffle://other/x"), .cancel)
        XCTAssertEqual(decide("about:blank"), .cancel)
        // HTML previews: a sandboxed srcdoc frame, never the chat document itself.
        XCTAssertEqual(decide("about:srcdoc", main: false, tap: false), .allow)
        XCTAssertEqual(decide("about:srcdoc"), .cancel)
        XCTAssertEqual(decide("about:blank", main: false), .cancel)
        XCTAssertEqual(decide(nil), .cancel)
    }

    func testWorkerURLLengthLimit() {
        let long = URL(string: "https://worker.example/api/" + String(repeating: "a", count: 8192))!
        XCTAssertNil(ChatPolicy.workerURL(base: base, url: long))
    }

    /// Android offers any main-frame HTTPS navigation with a gesture; WebKit reports
    /// `window.open` and `location` changes from a tap handler as `.other`.
    func testScriptOpenedLinksCountOnlyFromTheBundleMainFrame() {
        XCTAssertTrue(ChatPolicy.userInitiated(.linkTap, fromBundleMainFrame: false))
        XCTAssertTrue(ChatPolicy.userInitiated(.script, fromBundleMainFrame: true))
        XCTAssertTrue(ChatPolicy.userInitiated(.newWindow, fromBundleMainFrame: true))
        XCTAssertFalse(ChatPolicy.userInitiated(.script, fromBundleMainFrame: false))
        XCTAssertFalse(ChatPolicy.userInitiated(.newWindow, fromBundleMainFrame: false))
        XCTAssertFalse(ChatPolicy.userInitiated(.other, fromBundleMainFrame: true))

        let script = ChatPolicy.userInitiated(.script, fromBundleMainFrame: true)
        XCTAssertEqual(ChatPolicy.navigation(base: base, url: URL(string: "https://docs.example/x"), mainFrame: true,
                                             userInitiated: script),
                       .openExternally(URL(string: "https://docs.example/x")!))
        let framed = ChatPolicy.userInitiated(.script, fromBundleMainFrame: false)
        XCTAssertEqual(ChatPolicy.navigation(base: base, url: URL(string: "https://docs.example/x"), mainFrame: true,
                                             userInitiated: framed), .cancel)
    }

    func testBundleOrigin() {
        XCTAssertTrue(ChatPolicy.isBundleOrigin(scheme: "aishuffle", host: "app", port: 0))
        XCTAssertTrue(ChatPolicy.isBundleOrigin(scheme: "AIShuffle", host: "APP", port: 0))
        XCTAssertFalse(ChatPolicy.isBundleOrigin(scheme: "aishuffle", host: "app.evil", port: 0))
        XCTAssertFalse(ChatPolicy.isBundleOrigin(scheme: "aishuffle", host: "app", port: 8443))
        XCTAssertFalse(ChatPolicy.isBundleOrigin(scheme: "https", host: "app", port: 0))
        XCTAssertFalse(ChatPolicy.isBundleOrigin(scheme: "", host: "", port: 0))
    }

    func testMicrophoneOnlyForTheBundleDocument() {
        let page = URL(string: "aishuffle://app/assets/mobile-ui/")
        func allowed(top: URL? = page, scheme: String = "aishuffle", host: String = "app", port: Int = 0,
                     main: Bool = true, micOnly: Bool = true, authorized: Bool = true) -> Bool {
            ChatPolicy.microphone(topLevel: top, requestScheme: scheme, requestHost: host, requestPort: port,
                                  mainFrame: main, microphoneOnly: micOnly, authorized: authorized)
        }
        XCTAssertTrue(allowed())
        XCTAssertFalse(allowed(top: URL(string: "https://worker.example/")))
        XCTAssertFalse(allowed(top: nil))
        XCTAssertFalse(allowed(scheme: "https", host: "worker.example"))
        XCTAssertFalse(allowed(host: "other"))
        XCTAssertFalse(allowed(port: 8443))
        XCTAssertFalse(allowed(main: false))
        XCTAssertFalse(allowed(micOnly: false))
        XCTAssertFalse(allowed(authorized: false))
    }

    func testContentRulesBlockEverythingButTheBundleAndWorker() throws {
        let source = try ChatPolicy.contentRules(base: "https://Worker.Example:8443/")
        let rules = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(source.utf8)) as? [[String: [String: String]]])
        XCTAssertEqual(rules.first?["trigger"]?["url-filter"], ".*")
        XCTAssertEqual(rules.first?["action"]?["type"], "block")
        XCTAssertEqual(rules.dropFirst().map { $0["action"]?["type"] }, Array(repeating: "ignore-previous-rules", count: 5))
        XCTAssertEqual(rules.dropFirst().compactMap { $0["trigger"]?["url-filter"] }, [
            #"^aishuffle://app/"#,
            #"^https://worker\.example:8443/"#,
            #"^wss://worker\.example:8443/"#,
            "^data:",
            "^blob:",
        ])
        let ipv6 = try ChatPolicy.contentRules(base: "https://[::1]")
        XCTAssertTrue(ipv6.contains(#"^https://\\[::1\\]/"#), ipv6)
        XCTAssertThrowsError(try ChatPolicy.contentRules(base: "http://worker.example"))
    }
}

final class BundleAssetsTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("_app"), withIntermediateDirectories: true)
        try Data("<html></html>".utf8).write(to: root.appendingPathComponent("index.html"))
        try Data("x".utf8).write(to: root.appendingPathComponent("_app/start.js"))
        try Data("secret".utf8).write(to: root.deletingLastPathComponent().appendingPathComponent("\(root.lastPathComponent)-secret.txt"))
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
        try? FileManager.default.removeItem(at: root.deletingLastPathComponent().appendingPathComponent("\(root.lastPathComponent)-secret.txt"))
    }

    private func file(_ raw: String) -> String? {
        BundleAssets.file(for: URL(string: raw)!, root: root)?.lastPathComponent
    }

    func testServesBundleFilesAndTheIndex() {
        XCTAssertEqual(file("aishuffle://app/assets/mobile-ui/"), "index.html")
        XCTAssertEqual(file("aishuffle://app/assets/mobile-ui"), "index.html")
        XCTAssertEqual(file("aishuffle://app/assets/mobile-ui/_app/start.js?v=1"), "start.js")
    }

    func testBlocksEverythingElse() {
        let secret = "\(root.lastPathComponent)-secret.txt"
        for raw in ["aishuffle://app/assets/mobile-ui/missing.js",
                    "aishuffle://app/assets/mobile-ui/_app",
                    "aishuffle://app/assets/mobile-ui/_app/",
                    "aishuffle://app/assets/mobile-ui/../\(secret)",
                    "aishuffle://app/assets/mobile-ui/%2e%2e/\(secret)",
                    "aishuffle://app/assets/mobile-ui/_app%2F..%2F..%2F\(secret)",
                    "aishuffle://app/assets/mobile-ui//index.html",
                    "aishuffle://app/api/v1/files/a.png",
                    "aishuffle://app/index.html",
                    "aishuffle://other/assets/mobile-ui/index.html",
                    "https://app/assets/mobile-ui/index.html"] {
            XCTAssertNil(file(raw), raw)
        }
    }

    func testMimeTypes() {
        XCTAssertEqual(BundleAssets.mimeType(for: URL(fileURLWithPath: "/a/index.html")), "text/html; charset=utf-8")
        XCTAssertEqual(BundleAssets.mimeType(for: URL(fileURLWithPath: "/a/x.js")), "text/javascript; charset=utf-8")
        XCTAssertEqual(BundleAssets.mimeType(for: URL(fileURLWithPath: "/a/x.woff2")), "font/woff2")
        XCTAssertEqual(BundleAssets.mimeType(for: URL(fileURLWithPath: "/a/x.unknown")), "application/octet-stream")
    }
}

@MainActor
final class ChatModelTests: XCTestCase {
    private let server = Server(id: "1", name: "Desk", baseUrl: "https://worker.example", apiKey: "aim_secret", deviceId: "d")

    func testExternalLinksPromptOneAtATimeAndOnlyWhileAuthorized() throws {
        let store = ServerStore(storage: MemoryStorage())
        try store.add(server)
        let model = ChatModel(server: server, store: store)
        let first = URL(string: "https://a.example/")!
        model.offerExternalLink(first)
        model.offerExternalLink(URL(string: "https://b.example/")!)
        XCTAssertEqual(model.externalLink, first)
        model.externalLink = nil
        try store.remove(id: server.id)
        model.offerExternalLink(first)
        XCTAssertNil(model.externalLink)
    }

    /// A covered chat (camera picker, cancelled swipe-back) stays open; a popped one closes.
    func testChatClosesOnlyWhenItLeavesTheNavigationPath() {
        XCTAssertFalse(ChatModel.shouldClose(path: ["1"], serverId: "1"))
        XCTAssertFalse(ChatModel.shouldClose(path: ["2", "1"], serverId: "1"))
        XCTAssertTrue(ChatModel.shouldClose(path: [], serverId: "1"))
        XCTAssertTrue(ChatModel.shouldClose(path: ["2"], serverId: "1"))
    }
}

@MainActor
final class ServerStoreTests: XCTestCase {
    private let server = Server(id: "1", name: "Desk", baseUrl: "https://worker.example", apiKey: "aim_secret", deviceId: "d")

    func testPersistsAndChecksAuthorization() throws {
        let storage = MemoryStorage()
        let store = ServerStore(storage: storage)
        try store.add(server)
        XCTAssertEqual(ServerStore(storage: storage).servers, [server])
        XCTAssertTrue(store.isCurrent(server))
        var renamed = server
        renamed.name = "Renamed"
        XCTAssertTrue(store.isCurrent(renamed))
        let rekeyed = Server(id: "1", name: "Desk", baseUrl: "https://worker.example", apiKey: "aim_other", deviceId: "d")
        XCTAssertFalse(store.isCurrent(rekeyed))
        try store.remove(id: "1")
        XCTAssertFalse(store.isCurrent(server))
        XCTAssertFalse(String(describing: server).contains("aim_secret"))
    }

    func testUnreadableStoreIsNeverOverwritten() {
        let storage = MemoryStorage(Data("not json".utf8))
        let store = ServerStore(storage: storage)
        XCTAssertNotNil(store.loadError)
        XCTAssertThrowsError(try store.add(server))
        XCTAssertEqual(storage.data, Data("not json".utf8))
    }
}
