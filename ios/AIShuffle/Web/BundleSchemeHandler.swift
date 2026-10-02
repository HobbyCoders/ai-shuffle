import Foundation
import WebKit

/// Maps `aishuffle://app/assets/mobile-ui/...` to files of the bundled mobile UI. Pure, so it is
/// unit-tested without a WebView.
enum BundleAssets {
    /// The bundled UI directory inside the app, or nil when this build lacks it.
    static var root: URL? {
        Bundle.main.resourceURL?.appendingPathComponent(MobileUI.assetDirectory, isDirectory: true)
    }

    /// The file to serve for `url`, or nil (blocked) for another origin, another path,
    /// a dot segment, or anything that is not a regular file inside `root`.
    static func file(for url: URL, root: URL) -> URL? {
        guard MobileUI.isAssetOrigin(url), let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        let path = parts.path
        let relative: String
        if MobileUI.isIndexPath(path) {
            relative = "index.html"
        } else {
            guard path.hasPrefix(MobileUI.path) else { return nil }
            relative = String(path.dropFirst(MobileUI.path.count))
        }
        let segments = relative.split(separator: "/", omittingEmptySubsequences: false)
        guard !segments.isEmpty, segments.allSatisfy({ segment in
            !segment.isEmpty && segment != "." && segment != ".." &&
                !segment.contains("\\") && !segment.contains("\0")
        }) else { return nil }
        let base = root.standardizedFileURL.resolvingSymlinksInPath()
        let candidate = base.appendingPathComponent(relative).standardizedFileURL.resolvingSymlinksInPath()
        guard candidate.path.hasPrefix(base.path + "/") else { return nil }
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: candidate.path, isDirectory: &directory), !directory.boolValue else { return nil }
        return candidate
    }

    static func mimeType(for file: URL) -> String {
        switch file.pathExtension.lowercased() {
        case "html", "htm": return "text/html; charset=utf-8"
        case "js", "mjs": return "text/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json", "map": return "application/json; charset=utf-8"
        case "webmanifest": return "application/manifest+json; charset=utf-8"
        case "txt": return "text/plain; charset=utf-8"
        case "svg": return "image/svg+xml"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "gif": return "image/gif"
        case "webp": return "image/webp"
        case "ico": return "image/x-icon"
        case "woff": return "font/woff"
        case "woff2": return "font/woff2"
        case "ttf": return "font/ttf"
        case "otf": return "font/otf"
        case "wasm": return "application/wasm"
        default: return "application/octet-stream"
        }
    }
}

/// The iOS counterpart of Android's WebViewAssetLoader: serves the bundled UI from the app
/// bundle under the `aishuffle` scheme. Only GET/HEAD of existing bundle files succeed;
/// everything else, including bundle-origin `/api/` paths, gets a 403.
final class BundleSchemeHandler: NSObject, WKURLSchemeHandler {
    private let root: URL

    init(root: URL) {
        self.root = root
    }

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        let request = task.request
        guard let url = request.url else {
            task.didFailWithError(URLError(.badURL))
            return
        }
        let method = request.httpMethod ?? "GET"
        guard method == "GET" || method == "HEAD" else {
            respond(task, url: url, status: 405, type: "text/plain; charset=utf-8", body: Data("Blocked".utf8))
            return
        }
        guard let file = BundleAssets.file(for: url, root: root), let data = try? Data(contentsOf: file) else {
            let message = MobileUI.isIndexPath(url.path) && MobileUI.isAssetOrigin(url)
                ? "The chat UI is missing from this build of the app. Reinstall AI Shuffle."
                : "This content is blocked."
            respond(task, url: url, status: 403, type: "text/plain; charset=utf-8", body: Data(message.utf8))
            return
        }
        respond(task, url: url, status: 200, type: BundleAssets.mimeType(for: file), body: method == "HEAD" ? Data() : data,
                length: data.count)
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        // Responses are delivered synchronously in start; nothing is left to cancel.
    }

    private func respond(_ task: WKURLSchemeTask, url: URL, status: Int, type: String, body: Data, length: Int? = nil) {
        let headers = [
            "Content-Type": type,
            "Content-Length": String(length ?? body.count),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        ]
        guard let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers) else {
            task.didFailWithError(URLError(.cannotParseResponse))
            return
        }
        task.didReceive(response)
        if !body.isEmpty { task.didReceive(body) }
        task.didFinish()
    }
}
