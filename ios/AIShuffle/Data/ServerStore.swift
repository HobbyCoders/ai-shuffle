import Foundation
import Security

/// A paired worker. `apiKey` is the device key from `/api/v1/mobile/register`; it never
/// leaves the Keychain except as a Bearer header to this worker's own origin.
struct Server: Codable, Identifiable, Equatable, CustomStringConvertible {
    let id: String
    var name: String
    let baseUrl: String
    let apiKey: String
    let deviceId: String

    var host: String { String(baseUrl.dropFirst("https://".count)) }

    /// Keeps the key out of logs and debugger descriptions.
    var description: String { "Server(id: \(id), name: \(name), host: \(host))" }

    /// A rename does not change who may use this chat; a new key or address does.
    func sameAuthorization(_ other: Server?) -> Bool {
        guard let other else { return false }
        return id == other.id && baseUrl == other.baseUrl && apiKey == other.apiKey && deviceId == other.deviceId
    }
}

protocol SecretStorage {
    func read() throws -> Data?
    func write(_ data: Data) throws
}

/// One generic-password item holding the server list, readable only on this device after first unlock.
struct KeychainStorage: SecretStorage {
    var service = "com.hobbycoders.aishuffle.servers"
    var account = "servers"

    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    func read() throws -> Data? {
        var lookup = query
        lookup[kSecReturnData as String] = true
        lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        let status = SecItemCopyMatching(lookup as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw ShuffleError("Saved workers could not be read (Keychain \(status)).") }
        return data
    }

    func write(_ data: Data) throws {
        let attributes: [String: Any] = [kSecValueData as String: data,
                                         kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item.merge(attributes) { _, new in new }
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw ShuffleError("The worker could not be saved (Keychain \(status)).") }
    }
}

final class MemoryStorage: SecretStorage {
    var data: Data?
    init(_ data: Data? = nil) { self.data = data }
    func read() throws -> Data? { data }
    func write(_ data: Data) throws { self.data = data }
}

@MainActor
final class ServerStore: ObservableObject {
    @Published private(set) var servers: [Server] = []
    @Published private(set) var loadError: String?
    private let storage: SecretStorage

    init(storage: SecretStorage = KeychainStorage()) {
        self.storage = storage
        do {
            if let data = try storage.read() { servers = try JSONDecoder().decode([Server].self, from: data) }
        } catch {
            loadError = (error as? ShuffleError)?.message ?? "Saved workers could not be read."
        }
    }

    func server(id: String) -> Server? { servers.first { $0.id == id } }

    /// The chat for `server` is still authorized: the same saved credential exists.
    func isCurrent(_ server: Server) -> Bool { server.sameAuthorization(self.server(id: server.id)) }

    func add(_ server: Server) throws {
        try save(servers.filter { $0.id != server.id } + [server])
    }

    func remove(id: String) throws {
        try save(servers.filter { $0.id != id })
    }

    private func save(_ next: [Server]) throws {
        // Refuse to overwrite a list that could not be read.
        if loadError != nil { throw ShuffleError("Saved workers could not be read, so nothing was changed.") }
        try storage.write(try JSONEncoder().encode(next))
        servers = next
    }
}
