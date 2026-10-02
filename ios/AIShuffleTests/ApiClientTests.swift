import XCTest
@testable import AIShuffle

/// Answers ApiClient requests from a script, recording what was sent. No network.
final class StubProtocol: URLProtocol {
    struct Sent {
        let url: String
        let method: String
        let headers: [String: String]
        let body: Data
    }

    static var replies: [(status: Int, type: String, body: String)] = []
    static var sent: [Sent] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(buffer, count: count)
            }
            stream.close()
        }
        Self.sent.append(Sent(url: request.url?.absoluteString ?? "", method: request.httpMethod ?? "",
                              headers: request.allHTTPHeaderFields ?? [:], body: body))
        guard !Self.replies.isEmpty else {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
        let reply = Self.replies.removeFirst()
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": reply.type])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(reply.body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class ApiClientTests: XCTestCase {
    private let reservation = "wsm_" + String(repeating: "A", count: 43)
    private let server = Server(id: "1", name: "Desk", baseUrl: "https://worker.example", apiKey: "aim_key",
                                deviceId: "7c9e6679-7425-40de-944b-e07fc1f90ae7")

    private func client(_ replies: [(Int, String)]) -> ApiClient {
        StubProtocol.sent = []
        StubProtocol.replies = replies.map { (status: $0.0, type: "application/json", body: $0.1) }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubProtocol.self]
        return ApiClient(configuration: configuration)
    }

    private func json(_ data: Data) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }

    func testRegisterSendsAnIOSDeviceAndTheNormalizedCodeWithoutAuthorization() async throws {
        let api = client([(200, #"{"api_key":"aim_new","device_id":"dev-1"}"#)])
        let result = try await api.register(baseUrl: "https://Worker.Example/", deviceName: "Apple iPhone",
                                            deviceId: "dev-0", pairingCode: "abcd-efgh-1234")
        XCTAssertEqual(result, ApiClient.Registration(apiKey: "aim_new", deviceId: "dev-1"))
        let sent = try XCTUnwrap(StubProtocol.sent.first)
        XCTAssertEqual(sent.url, "https://worker.example/api/v1/mobile/register")
        XCTAssertEqual(sent.method, "POST")
        XCTAssertNil(sent.headers["Authorization"])
        let body = json(sent.body)
        XCTAssertEqual(body["pairing_code"] as? String, "ABCDEFGH1234")
        let device = try XCTUnwrap(body["device_info"] as? [String: Any])
        XCTAssertEqual(device["platform"] as? String, "ios")
        XCTAssertEqual(device["device_id"] as? String, "dev-0")
    }

    func testRegisterRejectsAProxyPage() async {
        let api = client([(200, "<html>login</html>")])
        do {
            _ = try await api.register(baseUrl: "https://worker.example", deviceName: "x", deviceId: "d", pairingCode: "ABCDEFGH1234")
            XCTFail("expected a failure")
        } catch {
            XCTAssertEqual((error as? ShuffleError)?.message, "The server returned a web page instead of data. It may be behind a login or proxy.")
        }
    }

    func testWebSessionReservesThenCommits() async throws {
        let api = client([
            (200, #"{"reservation":"\#(reservation)","expires_at":"2026-10-01T00:00:00Z","expires_in":30}"#),
            (200, #"{"session_token":"shc_token-1"}"#),
        ])
        let token = try await api.webSession(server)
        XCTAssertEqual(token, "shc_token-1")
        XCTAssertEqual(StubProtocol.sent.map(\.url), [
            "https://worker.example/api/v1/mobile/web-session/reservations",
            "https://worker.example/api/v1/mobile/web-session",
        ])
        XCTAssertEqual(StubProtocol.sent.map { $0.headers["Authorization"] }, ["Bearer aim_key", "Bearer aim_key"])
        XCTAssertEqual(StubProtocol.sent[1].headers[ApiClient.reservationHeader], reservation)
        XCTAssertEqual(json(StubProtocol.sent[1].body)["purpose"] as? String, "web")
    }

    func testWebSessionFallsBackOnWorkersWithoutReservations() async throws {
        let api = client([(404, #"{"detail":"Not Found"}"#), (200, #"{"session_token":"shc_legacy"}"#)])
        let token = try await api.webSession(server)
        XCTAssertEqual(token, "shc_legacy")
        XCTAssertNil(StubProtocol.sent[1].headers[ApiClient.reservationHeader])
    }

    func testWebSessionRetriesOneConflict() async throws {
        let ready = #"{"reservation":"\#(reservation)","expires_at":"x","expires_in":30}"#
        let api = client([(200, ready), (409, "{}"), (200, ready), (409, "{}")])
        do {
            _ = try await api.webSession(server)
            XCTFail("expected a failure")
        } catch {
            XCTAssertEqual((error as? ShuffleError)?.status, 409)
        }
        XCTAssertEqual(StubProtocol.sent.count, 4)
    }

    func testWebSessionRefusesAnUnsafeToken() async {
        let api = client([(404, #"{"detail":"Not Found"}"#), (200, #"{"session_token":"bad'token"}"#)])
        do {
            _ = try await api.webSession(server)
            XCTFail("expected a failure")
        } catch {
            XCTAssertEqual((error as? ShuffleError)?.message, "The worker returned an invalid chat authorization. Update the worker and retry.")
        }
    }

    func testReservationParsing() throws {
        func parse(_ status: Int, _ body: String, type: String = "application/json") throws -> ApiClient.Reservation {
            try ApiClient.parseReservation(status: status, contentType: type, body: Data(body.utf8), deviceId: nil)
        }
        XCTAssertEqual(try parse(405, #"{"detail":"Method Not Allowed"}"#), .unsupported)
        XCTAssertEqual(try parse(200, #"{"reservation":"\#(reservation)","expires_at":"x","expires_in":120}"#),
                       .ready(token: reservation, lifetime: 120))
        // A 404 from something other than FastAPI's router (a proxy page) is a failure, not an old worker.
        XCTAssertThrowsError(try parse(404, #"{"detail":"Not Found"}"#, type: "text/html"))
        XCTAssertThrowsError(try parse(404, #"{"detail":"Session not found"}"#))
        for body in [#"{"reservation":"wsm_short","expires_at":"x","expires_in":30}"#,
                     #"{"reservation":"\#(reservation)","expires_at":"x","expires_in":121}"#,
                     #"{"reservation":"\#(reservation)","expires_in":30}"#] {
            XCTAssertThrowsError(try parse(200, body), body)
        }
        XCTAssertThrowsError(try parse(200, String(repeating: " ", count: 8193)))
    }

    func testErrorMessages() {
        let confirm = Data(#"{"detail":"Confirm this device on the worker before connecting"}"#.utf8)
        XCTAssertTrue(ApiClient.errorMessage(status: 403, body: confirm, deviceId: server.deviceId).contains(server.deviceId))
        XCTAssertTrue(ApiClient.errorMessage(status: 403, body: confirm, deviceId: "not-a-uuid").contains("unavailable"))
        XCTAssertEqual(ApiClient.errorMessage(status: 401, body: Data(), deviceId: nil), "Authorization expired or was revoked. Pair this phone again.")
        XCTAssertTrue(ApiClient.errorMessage(status: 302, body: Data(), deviceId: nil).contains("redirected"))
    }
}
