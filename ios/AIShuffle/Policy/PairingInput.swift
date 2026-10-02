import Foundation

/// A user-facing validation or network failure. The message is safe to show.
struct ShuffleError: LocalizedError, Equatable {
    let message: String
    var status: Int?

    init(_ message: String, status: Int? = nil) {
        self.message = message
        self.status = status
    }

    var errorDescription: String? { message }
}

/// Only origins are accepted. A pairing link carries a short-lived code, never a credential.
/// Mirrors android/app/.../account/PairingInput.kt.
enum PairingInput {
    struct Pair: Equatable {
        let origin: String
        let code: String
    }

    /// `https://host[:port]`, lowercased, default port dropped. Hosts are limited to ASCII
    /// DNS names and bracketed IPv6 literals, which also keeps them safe inside the chat
    /// WebView's content-rule patterns.
    static func origin(_ raw: String) throws -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parts = URLComponents(string: trimmed) else {
            throw ShuffleError("Enter a valid HTTPS address.")
        }
        guard parts.scheme?.lowercased() == "https", let rawHost = parts.host, !rawHost.isEmpty else {
            throw ShuffleError("Use an HTTPS address with a host.")
        }
        guard parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.percentEncodedPath.isEmpty || parts.percentEncodedPath == "/" else {
            throw ShuffleError("Enter only the HTTPS origin, without a path, password, query or fragment.")
        }
        guard let host = normalizedHost(rawHost) else {
            throw ShuffleError("Use an HTTPS address with a host.")
        }
        if let port = parts.port {
            guard (1...65535).contains(port) else { throw ShuffleError("Invalid HTTPS port.") }
            return port == 443 ? "https://\(host)" : "https://\(host):\(port)"
        }
        return "https://\(host)"
    }

    static func code(_ raw: String) throws -> String {
        let value = raw.replacingOccurrences(of: " ", with: "")
            .replacingOccurrences(of: "-", with: "")
            .uppercased()
        guard value.count == 12, value.allSatisfy({ codeCharacters.contains($0) }) else {
            throw ShuffleError("Enter the 12-character pairing code. Spaces and hyphens are optional.")
        }
        return value
    }

    /// The pairing link a worker shows: `https://worker/pair#token=XXXXXXXXXXXX`
    /// (app/core/pairing.py build_pairing_url; Android's PairingInput.parseDirect).
    static func parse(_ raw: String) throws -> Pair {
        guard let parts = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme, let host = parts.host else {
            throw ShuffleError("Enter the address and code manually, or paste the full pairing link.")
        }
        guard parts.percentEncodedPath == "/pair", parts.query == nil, parts.user == nil, parts.password == nil else {
            throw ShuffleError("This is not the expected pairing link. Use /pair#token= followed by a pairing code.")
        }
        let fragment = parts.percentEncodedFragment ?? ""
        let prefix = "token="
        guard fragment.hasPrefix(prefix), !fragment.contains("&") else {
            throw ShuffleError("The link must contain only a pairing code.")
        }
        var authority = bracketed(host)
        if let port = parts.port { authority += ":\(port)" }
        return Pair(origin: try origin("\(scheme)://\(authority)"), code: try code(String(fragment.dropFirst(prefix.count))))
    }

    static func sameOrigin(_ base: String, _ candidate: String) -> Bool {
        guard let parts = URLComponents(string: candidate), parts.user == nil, parts.password == nil,
              let scheme = parts.scheme, let host = parts.host else { return false }
        var authority = bracketed(host)
        if let port = parts.port { authority += ":\(port)" }
        guard let left = try? origin("\(scheme)://\(authority)"), let right = try? origin(base) else { return false }
        return left == right
    }

    private static let codeCharacters = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

    private static func bracketed(_ host: String) -> String {
        host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
    }

    private static func normalizedHost(_ raw: String) -> String? {
        let host = bracketed(raw.lowercased())
        if host.hasPrefix("[") {
            let inner = host.dropFirst().dropLast()
            guard host.hasSuffix("]"), !inner.isEmpty,
                  inner.allSatisfy({ $0.isHexDigit || $0 == ":" || $0 == "." }) else { return nil }
            return host
        }
        let allowed = Set("abcdefghijklmnopqrstuvwxyz0123456789-.")
        guard host.count <= 253, host.allSatisfy({ allowed.contains($0) }),
              !host.hasPrefix("."), !host.hasSuffix("."), !host.contains("..") else { return nil }
        return host
    }
}
