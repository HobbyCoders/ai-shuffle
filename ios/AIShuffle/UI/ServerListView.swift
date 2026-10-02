import SwiftUI

/// Paired workers. Account sign-in, QR pairing and presence checks from the Android app are
/// not ported yet; workers are paired by address and code (AddServerView).
struct ServerListView: View {
    @EnvironmentObject private var store: ServerStore
    @State private var pairing = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                if let message = store.loadError ?? error {
                    Text(message).font(.footnote).foregroundStyle(.red)
                }
                ForEach(store.servers) { server in
                    NavigationLink(value: server.id) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(server.name)
                            Text(server.host).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete(perform: remove)
            }
            .overlay {
                if store.servers.isEmpty && store.loadError == nil {
                    VStack(spacing: 12) {
                        Text("No workers paired yet.").foregroundStyle(.secondary)
                        Button("Pair a worker") { pairing = true }
                    }
                }
            }
            .navigationTitle("Workers")
            .navigationDestination(for: String.self) { id in
                if let server = store.server(id: id) {
                    ChatScreen(server: server, store: store)
                }
            }
            .toolbar {
                Button { pairing = true } label: { Image(systemName: "plus") }
                    .accessibilityLabel("Pair a worker")
            }
            .sheet(isPresented: $pairing) {
                AddServerView().environmentObject(store)
            }
        }
    }

    private func remove(at offsets: IndexSet) {
        do {
            for index in offsets { try store.remove(id: store.servers[index].id) }
            error = nil
        } catch {
            self.error = (error as? ShuffleError)?.message ?? "The worker could not be removed."
        }
    }
}
