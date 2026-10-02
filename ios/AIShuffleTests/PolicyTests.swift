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
        XCTAssertFalse(PairingInput.sameOrigin(base, "aishuffle://app/mobile/"))
    }
}

final class MobileUITests: XCTestCase {
    private func config(_ script: String) throws -> [String: Any] {
        let start = try XCTUnwrap(script.range(of: "window.__SHUFFLE_MOBILE__ = "))
        let end = try XCTUnwrap(script.range(of: "; }", options: .backwards))
        let json = String(script[start.upperBound..<end.lowerBound])
        return try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
    }

    func testBootstrapScriptIsGuardedToTheWorkersPhoneUi() throws {
        let script = try MobileUI.bootstrapScript(apiOrigin: "https://Worker.Example:443", token: "shc_abc-DEF_123",
                                                  version: " 0.9.0 ", deviceName: "iPhone")
        XCTAssertTrue(script.hasPrefix(#"if (window.top === window && location.origin === "https://worker.example" && location.pathname.indexOf("/mobile/") === 0) { "#), script)
        let values = try config(script)
        let native = try XCTUnwrap(values["native"] as? [String: Any])
        XCTAssertEqual(native["platform"] as? String, "ios")
        XCTAssertEqual(native["bridge"] as? Int, MobileUI.bridgeVersion)
        XCTAssertEqual(native["capabilities"] as? [String], MobileUI.capabilities)
        XCTAssertFalse(MobileUI.capabilities.contains("file-save"), "iOS has no save bridge yet")
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

    func testTheWorkerServesThePhoneUiAtMobile() throws {
        XCTAssertEqual(try MobileUI.indexURL(base: "https://Worker.Example/").absoluteString, "https://worker.example/mobile/")
        XCTAssertEqual(try MobileUI.indexURL(base: "https://worker.example:8443").absoluteString, "https://worker.example:8443/mobile/")
        XCTAssertThrowsError(try MobileUI.indexURL(base: "http://worker.example"))
    }

    func testOnlyThePairedWorkersPhoneUiCounts() {
        let base = "https://worker.example"
        for raw in ["https://worker.example/mobile/", "https://worker.example/mobile", "https://worker.example/mobile/chat/abc?x=1",
                    "https://WORKER.example:443/mobile/_app/immutable/start.js"] {
            XCTAssertTrue(MobileUI.isUiURL(base: base, url: URL(string: raw)), raw)
        }
        for raw in ["https://worker.example/", "https://worker.example/chat", "https://worker.example/mobilex",
                    "https://worker.example/api/v1/auth/status", "https://worker.example/mobile/../api/v1/x",
                    "https://worker.example/mobile/%2E%2E/api", "http://worker.example/mobile/", "https://worker.example:8443/mobile/",
                    "https://worker.example.evil/mobile/", "https://user@worker.example/mobile/",
                    "aishuffle://app/assets/mobile-ui/", "about:blank"] {
            XCTAssertFalse(MobileUI.isUiURL(base: base, url: URL(string: raw)), raw)
        }
        XCTAssertFalse(MobileUI.isUiURL(base: base, url: nil))
    }

    func testAnOldWorkerGetsAnUpdateMessage() {
        XCTAssertTrue(MobileUI.loadFailure(status: 404).contains("Update the worker"))
        XCTAssertTrue(MobileUI.loadFailure(status: 502).contains("HTTP 502"))
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
        XCTAssertEqual(decide("https://worker.example/mobile/", tap: false), .allow)
        XCTAssertEqual(decide("https://worker.example/mobile/chat/1"), .allow)
        XCTAssertEqual(decide("https://worker.example/api/v1/files/a.png"),
                       .workerFile(URL(string: "https://worker.example/api/v1/files/a.png")!))
        // Another worker page (the desktop UI) is never the chat document.
        XCTAssertEqual(decide("https://worker.example/chat"), .workerFile(URL(string: "https://worker.example/chat")!))
        XCTAssertEqual(decide("aishuffle://app/assets/mobile-ui/"), .cancel)
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
    func testScriptOpenedLinksCountOnlyFromThePhoneUiMainFrame() {
        XCTAssertTrue(ChatPolicy.userInitiated(.linkTap, fromUiMainFrame: false))
        XCTAssertTrue(ChatPolicy.userInitiated(.script, fromUiMainFrame: true))
        XCTAssertTrue(ChatPolicy.userInitiated(.newWindow, fromUiMainFrame: true))
        XCTAssertFalse(ChatPolicy.userInitiated(.script, fromUiMainFrame: false))
        XCTAssertFalse(ChatPolicy.userInitiated(.newWindow, fromUiMainFrame: false))
        XCTAssertFalse(ChatPolicy.userInitiated(.other, fromUiMainFrame: true))

        let script = ChatPolicy.userInitiated(.script, fromUiMainFrame: true)
        XCTAssertEqual(ChatPolicy.navigation(base: base, url: URL(string: "https://docs.example/x"), mainFrame: true,
                                             userInitiated: script),
                       .openExternally(URL(string: "https://docs.example/x")!))
        let framed = ChatPolicy.userInitiated(.script, fromUiMainFrame: false)
        XCTAssertEqual(ChatPolicy.navigation(base: base, url: URL(string: "https://docs.example/x"), mainFrame: true,
                                             userInitiated: framed), .cancel)
    }

    func testWorkerOrigin() {
        XCTAssertTrue(ChatPolicy.isWorkerOrigin(base: base, scheme: "https", host: "worker.example", port: 0))
        XCTAssertTrue(ChatPolicy.isWorkerOrigin(base: base, scheme: "HTTPS", host: "WORKER.example", port: 443))
        XCTAssertTrue(ChatPolicy.isWorkerOrigin(base: "https://[::1]:8443", scheme: "https", host: "::1", port: 8443))
        XCTAssertFalse(ChatPolicy.isWorkerOrigin(base: base, scheme: "https", host: "worker.example.evil", port: 0))
        XCTAssertFalse(ChatPolicy.isWorkerOrigin(base: base, scheme: "https", host: "worker.example", port: 8443))
        XCTAssertFalse(ChatPolicy.isWorkerOrigin(base: base, scheme: "http", host: "worker.example", port: 0))
        XCTAssertFalse(ChatPolicy.isWorkerOrigin(base: base, scheme: "aishuffle", host: "app", port: 0))
        XCTAssertFalse(ChatPolicy.isWorkerOrigin(base: base, scheme: "", host: "", port: 0))
    }

    func testMicrophoneOnlyForThePhoneUiDocument() {
        let page = URL(string: "https://worker.example/mobile/chat/1")
        func allowed(top: URL? = page, scheme: String = "https", host: String = "worker.example", port: Int = 0,
                     main: Bool = true, micOnly: Bool = true, authorized: Bool = true) -> Bool {
            ChatPolicy.microphone(base: base, topLevel: top, requestScheme: scheme, requestHost: host, requestPort: port,
                                  mainFrame: main, microphoneOnly: micOnly, authorized: authorized)
        }
        XCTAssertTrue(allowed())
        XCTAssertFalse(allowed(top: URL(string: "https://worker.example/")))
        XCTAssertFalse(allowed(top: nil))
        XCTAssertFalse(allowed(scheme: "aishuffle", host: "app"))
        XCTAssertFalse(allowed(host: "other"))
        XCTAssertFalse(allowed(port: 8443))
        XCTAssertFalse(allowed(main: false))
        XCTAssertFalse(allowed(micOnly: false))
        XCTAssertFalse(allowed(authorized: false))
    }

    func testContentRulesBlockEverythingButTheWorker() throws {
        let source = try ChatPolicy.contentRules(base: "https://Worker.Example:8443/")
        let rules = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(source.utf8)) as? [[String: [String: Any]]])
        XCTAssertEqual(rules.first?["trigger"]?["url-filter"] as? String, ".*")
        XCTAssertNil(rules.first?["trigger"]?["resource-type"])
        XCTAssertEqual(rules.first?["action"]?["type"] as? String, "block")
        XCTAssertEqual(rules.dropFirst().map { $0["action"]?["type"] as? String }, Array(repeating: "ignore-previous-rules", count: 5))
        XCTAssertEqual(rules.dropFirst().compactMap { $0["trigger"]?["url-filter"] as? String }, [
            #"^https://worker\.example:8443/"#,
            #"^wss://worker\.example:8443/"#,
            "^data:",
            "^blob:",
            "^https://",
        ])
        // Only the allowlist entries apply to every load; the HTTPS exception is for popups alone.
        XCTAssertEqual(rules.dropFirst().compactMap { $0["trigger"]?["resource-type"] as? [String] }, [["popup"]])
        XCTAssertNil(rules[4]["trigger"]?["resource-type"])
        XCTAssertEqual(rules[5]["trigger"]?["resource-type"] as? [String], ["popup"])
        let ipv6 = try ChatPolicy.contentRules(base: "https://[::1]")
        XCTAssertTrue(ipv6.contains(#"^https://\\[::1\\]/"#), ipv6)
        XCTAssertThrowsError(try ChatPolicy.contentRules(base: "http://worker.example"))
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
