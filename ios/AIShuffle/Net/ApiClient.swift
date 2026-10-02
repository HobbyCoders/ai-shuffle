import Foundation

/// Native calls to a worker's mobile API: pairing and minting the chat's Bearer session.
/// Same endpoints and checks as Android's ApiClient.kt. No cookies, no cache, and redirects
/// are never followed, so a credential cannot be replayed to another origin.
final class ApiClient {
    struct Registration: Equatable {
        let apiKey: String
        let deviceId: String
    }

    enum Reservation: Equatable {
        case unsupported
        case ready(token: String, lifetime: TimeInterval)
    }

    static let reservationHeader = "X-Shuffle-Web-Session-Reservation"
    static let unreachable = "Cannot reach this server. Check your HTTPS address and connection, then retry."
    private static let invalidReservation = "The worker returned an invalid session reservation."

    private let session: URLSession

    init(configuration: URLSessionConfiguration = .ephemeral) {
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 20
        session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }

    // MARK: Pairing

    func register(baseUrl: String, deviceName: String, deviceId: String, pairingCode: String) async throws -> Registration {
        let origin = try PairingInput.origin(baseUrl)
        let body: [String: Any] = [
            "device_info": [
                "device_id": deviceId,
                "device_name": String(deviceName.prefix(100)),
                "platform": "ios",
                "capabilities": ["supports_biometric": false, "supports_push": false, "biometric_types": [String]()] as [String: Any],
            ] as [String: Any],
            "pairing_code": try PairingInput.code(pairingCode),
        ]
        var request = URLRequest(url: URL(string: origin + "/api/v1/mobile/register")!)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let json = try Self.object(try await send(request), deviceId: nil)
        guard let key = json["api_key"] as? String, !key.isEmpty else {
            throw ShuffleError("The server returned an unsupported response. Update the server and retry.")
        }
        let assigned = (json["device_id"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? deviceId
        return Registration(apiKey: key, deviceId: assigned)
    }

    // MARK: Chat session

    /// A fresh paired-session token for the chat page: reserve, then commit; falls back to the
    /// single-call endpoint on workers without reservations, and retries once after a 409.
    func webSession(_ server: Server) async throws -> String {
        for attempt in 0..<2 {
            let started = ProcessInfo.processInfo.systemUptime
            let reply = try await send(reservationRequest(server))
            switch try Self.parseReservation(status: reply.status, contentType: reply.contentType, body: reply.body, deviceId: server.deviceId) {
            case .unsupported:
                if attempt != 0 { throw ShuffleError("The worker changed its session protocol. Update the worker and retry.") }
                return try Self.sessionToken(try await send(sessionRequest(server, reservation: nil)), deviceId: server.deviceId)
            case let .ready(token, lifetime):
                guard ProcessInfo.processInfo.systemUptime < started + lifetime else {
                    throw ShuffleError("The connection request expired. Refresh chat to try again.")
                }
                let committed = try await send(sessionRequest(server, reservation: token))
                if committed.status == 409 {
                    if attempt == 1 { throw ShuffleError("Another connection replaced this request. Refresh chat to try again.", status: 409) }
                    continue
                }
                return try Self.sessionToken(committed, deviceId: server.deviceId)
            }
        }
        throw ShuffleError("Another connection replaced this request. Refresh chat to try again.", status: 409)
    }

    func reservationRequest(_ server: Server) throws -> URLRequest {
        var request = URLRequest(url: URL(string: try PairingInput.origin(server.baseUrl) + "/api/v1/mobile/web-session/reservations")!)
        request.httpMethod = "POST"
        request.setValue("Bearer \(server.apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)
        return request
    }

    func sessionRequest(_ server: Server, reservation: String?) throws -> URLRequest {
        if let reservation, !Self.isReservation(reservation) { throw ShuffleError(Self.invalidReservation) }
        var request = URLRequest(url: URL(string: try PairingInput.origin(server.baseUrl) + "/api/v1/mobile/web-session")!)
        request.httpMethod = "POST"
        request.setValue("Bearer \(server.apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let reservation { request.setValue(reservation, forHTTPHeaderField: Self.reservationHeader) }
        request.httpBody = Data(#"{"purpose":"web"}"#.utf8)
        return request
    }

    static func parseReservation(status: Int, contentType: String?, body: Data, deviceId: String?) throws -> Reservation {
        guard body.count <= 8192 else { throw ShuffleError(invalidReservation, status: status) }
        let isJSON = contentType?.split(separator: ";").first?.trimmingCharacters(in: .whitespaces).lowercased() == "application/json"
        let json = isJSON ? (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] : nil
        let detail = json?["detail"] as? String
        if (status == 404 && detail == "Not Found") || (status == 405 && detail == "Method Not Allowed") { return .unsupported }
        guard status == 200 else { throw ShuffleError(errorMessage(status: status, body: body, deviceId: deviceId), status: status) }
        guard let token = json?["reservation"] as? String, isReservation(token),
              let expiry = json?["expires_at"] as? String, !expiry.isEmpty,
              let lifetime = json?["expires_in"] as? Int, (1...120).contains(lifetime) else {
            throw ShuffleError(invalidReservation, status: status)
        }
        return .ready(token: token, lifetime: TimeInterval(lifetime))
    }

    static func isReservation(_ value: String) -> Bool {
        let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        return value.hasPrefix("wsm_") && value.count == 47 && value.dropFirst(4).allSatisfy { allowed.contains($0) }
    }

    static func errorMessage(status: Int, body: Data, deviceId: String?) -> String {
        let detail = ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["detail"] as? String
        if status == 403 && detail == "Confirm this device on the worker before connecting" {
            // Our own saved enrollment identity, never an ID echoed by the worker.
            guard let id = deviceId, UUID(uuidString: id) != nil else {
                return "Waiting for worker confirmation. This phone's saved device ID is unavailable; do not approve by name. Reopen this worker and retry."
            }
            return "Waiting for worker confirmation. Phone device ID: \(id). Compare this exact ID on the worker before approving; a matching name is not proof. Ask the owner to approve this phone on the worker, then retry. Your saved credential has been kept."
        }
        if status == 403 && detail == "This device was denied by the worker owner" {
            return "The worker owner denied this phone. Contact the owner before enrolling a new device identity."
        }
        switch status {
        case 300..<400: return "This address redirected elsewhere. It may be behind a login gate such as Cloudflare Access, which the iOS app does not support yet."
        case 401: return "Authorization expired or was revoked. Pair this phone again."
        case 403: return "Pairing was refused. Check the code and this device’s access."
        case 404, 405: return "This server does not support this mobile action. Check the address and update the server."
        case 409, 410: return "This pairing code was used or expired. Generate a new code."
        default: return "The server could not complete this request (HTTP \(status)). Try again."
        }
    }

    // MARK: Transport

    struct Reply {
        let status: Int
        let contentType: String?
        let body: Data
    }

    private func send(_ request: URLRequest) async throws -> Reply {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw ShuffleError(Self.unreachable) }
            return Reply(status: http.statusCode, contentType: http.value(forHTTPHeaderField: "Content-Type"), body: data)
        } catch let error as ShuffleError {
            throw error
        } catch {
            throw ShuffleError(Self.unreachable)
        }
    }

    private static func object(_ reply: Reply, deviceId: String?) throws -> [String: Any] {
        guard (200..<300).contains(reply.status) else {
            throw ShuffleError(errorMessage(status: reply.status, body: reply.body, deviceId: deviceId), status: reply.status)
        }
        let text = String(decoding: reply.body, as: UTF8.self)
        // A 200 carrying HTML means a proxy or login page slipped through.
        if text.drop(while: { $0.isWhitespace }).hasPrefix("<") {
            throw ShuffleError("The server returned a web page instead of data. It may be behind a login or proxy.")
        }
        guard let json = (try? JSONSerialization.jsonObject(with: reply.body)) as? [String: Any] else {
            throw ShuffleError("The server returned an unsupported response. Update the server and retry.")
        }
        return json
    }

    private static func sessionToken(_ reply: Reply, deviceId: String?) throws -> String {
        let json = try object(reply, deviceId: deviceId)
        guard let token = json["session_token"] as? String, MobileUI.isValidToken(token) else {
            throw ShuffleError("The worker returned an invalid chat authorization. Update the worker and retry.")
        }
        return token
    }
}

private final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
