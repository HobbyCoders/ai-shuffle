import SwiftUI

@main
struct AIShuffleApp: App {
    @StateObject private var store = ServerStore()

    var body: some Scene {
        WindowGroup {
            ServerListView().environmentObject(store)
        }
    }
}
