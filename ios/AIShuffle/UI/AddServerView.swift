import SwiftUI
import UIKit

/// Pair by the worker's HTTPS address and its 12-character code, or by pasting the full
/// pairing link. QR scanning (VisionKit) is not ported yet.
struct AddServerView: View {
    @EnvironmentObject private var store: ServerStore
    @Environment(\.dismiss) private var dismiss
    @State private var address = ""
    @State private var code = ""
    @State private var name = ""
    @State private var status = ""
    @State private var busy = false
    private let api = ApiClient()

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://worker.example", text: $address)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textContentType(.URL)
                    TextField("Pairing code", text: $code)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                    TextField("Name (optional)", text: $name)
                } footer: {
                    Text("Enter the worker's HTTPS address and the 12-character pairing code it shows, or paste its pairing link.")
                }
                if !status.isEmpty {
                    Section { Text(status).font(.footnote) }
                }
            }
            .navigationTitle("Pair a worker")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Pair") { Task { await pair() } }.disabled(busy)
                }
            }
        }
    }

    private func pair() async {
        busy = true
        defer { busy = false }
        do {
            let target: PairingInput.Pair
            if address.contains("#") {
                target = try PairingInput.parse(address)
            } else {
                target = PairingInput.Pair(origin: try PairingInput.origin(address), code: try PairingInput.code(code))
            }
            status = "Connecting to the worker…"
            let registration = try await api.register(baseUrl: target.origin, deviceName: "Apple \(UIDevice.current.model)",
                                                      deviceId: UUID().uuidString.lowercased(), pairingCode: target.code)
            let label = name.trimmingCharacters(in: .whitespacesAndNewlines)
            let server = Server(id: UUID().uuidString, name: label.isEmpty ? String(target.origin.dropFirst("https://".count)) : label,
                                baseUrl: target.origin, apiKey: registration.apiKey, deviceId: registration.deviceId)
            try store.add(server)
            dismiss()
        } catch {
            status = (error as? ShuffleError)?.message ?? ApiClient.unreachable
        }
    }
}
