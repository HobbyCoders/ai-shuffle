import SwiftUI
import WebKit

/// Mints the paired session natively, then shows the bundled UI with it.
@MainActor
final class ChatModel: ObservableObject {
    @Published var status = ""
    @Published var externalLink: URL?
    @Published private(set) var controller: ChatWebController?
    @Published private(set) var closed = false
    let server: Server
    private let store: ServerStore
    private let api: ApiClient
    private var task: Task<Void, Never>?
    private var connected = false

    init(server: Server, store: ServerStore, api: ApiClient = ApiClient()) {
        self.server = server
        self.store = store
        self.api = api
    }

    private func authorized() -> Bool { !closed && store.isCurrent(server) }

    func start(reload: Bool = false) {
        guard task == nil else { return }
        guard authorized() else { close(); return }
        status = "Opening a secure chat…"
        task = Task { [weak self] in
            await self?.connect(reload: reload)
            self?.task = nil
        }
    }

    /// Back in the foreground: reconnect only if the last attempt did not finish.
    func resume() {
        if !connected { start() }
    }

    func close() {
        closed = true
        task?.cancel()
        controller?.close()
    }

    private func connect(reload: Bool) async {
        connected = false
        do {
            let web = try await webController()
            let token = try await api.webSession(server)
            guard !Task.isCancelled, authorized() else { return }
            try web.install(token: token)
            if reload || !MobileUI.isAssetOrigin(web.webView.url) { web.loadIndex() }
            connected = true
            status = ""
        } catch {
            if !Task.isCancelled { status = (error as? ShuffleError)?.message ?? ApiClient.unreachable }
        }
    }

    private func webController() async throws -> ChatWebController {
        if let controller { return controller }
        guard let root = BundleAssets.root else {
            throw ShuffleError("The chat UI is missing from this build of the app. Reinstall AI Shuffle.")
        }
        let rules = try await Self.compileRules(for: server)
        let web = ChatWebController(server: server, rules: rules, root: root) { [weak self] in self?.authorized() ?? false }
        web.onStatus = { [weak self] in self?.status = $0 }
        web.onExternalLink = { [weak self] in self?.externalLink = $0 }
        controller = web
        return web
    }

    /// Compiled before the WebView exists; a failure leaves the chat closed rather than unfiltered.
    static func compileRules(for server: Server) async throws -> WKContentRuleList {
        let source = try ChatPolicy.contentRules(base: server.baseUrl)
        guard let store = WKContentRuleListStore.default() else { throw ShuffleError("Chat could not start its network filter.") }
        return try await withCheckedThrowingContinuation { continuation in
            store.compileContentRuleList(forIdentifier: "chat-allowlist", encodedContentRuleList: source) { list, error in
                if let list { continuation.resume(returning: list) }
                else { continuation.resume(throwing: error ?? ShuffleError("Chat could not start its network filter.")) }
            }
        }
    }
}

struct ChatScreen: View {
    @StateObject private var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.openURL) private var openURL

    init(server: Server, store: ServerStore) {
        _model = StateObject(wrappedValue: ChatModel(server: server, store: store))
    }

    var body: some View {
        VStack(spacing: 0) {
            if !model.status.isEmpty {
                Text(model.status)
                    .font(.footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
            }
            if let controller = model.controller {
                ChatWebView(controller: controller)
            } else {
                Spacer()
            }
        }
        .navigationTitle(model.server.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            Menu {
                Button("Refresh chat") { model.start(reload: true) }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .accessibilityLabel("More chat options")
        }
        .confirmationDialog("Open outside AI Shuffle?", isPresented: Binding(
            get: { model.externalLink != nil }, set: { if !$0 { model.externalLink = nil } }
        ), titleVisibility: .visible) {
            Button("Open in browser") {
                if let url = model.externalLink { openURL(url) }
                model.externalLink = nil
            }
        } message: {
            Text("\(model.externalLink?.host ?? "This link") opens in your browser. No account or server credential will be added.")
        }
        .onAppear { model.start() }
        .onDisappear { model.close() }
        .onChange(of: model.closed) { closed in if closed { dismiss() } }
        .onChange(of: scenePhase) { phase in if phase == .active { model.resume() } }
    }
}

private struct ChatWebView: UIViewRepresentable {
    let controller: ChatWebController

    func makeUIView(context: Context) -> WKWebView { controller.webView }
    func updateUIView(_ view: WKWebView, context: Context) {}
}
