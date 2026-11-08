import AppKit
import WebKit

private struct LocalWorkspace {
    let url: URL
    let token: String

    init?(runtimeFile: URL) {
        guard let data = try? Data(contentsOf: runtimeFile),
              let info = try? JSONDecoder().decode([String: String].self, from: data),
              let address = info["url"],
              let url = URL(string: address),
              url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
              url.user == nil, url.password == nil, url.path == "/", url.query == nil,
              let fragment = url.fragment, fragment.hasPrefix("token=") else { return nil }
        let token = String(fragment.dropFirst("token=".count))
        guard token.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else { return nil }
        self.url = url
        self.token = token
    }

    func contains(_ destination: URL) -> Bool {
        destination.scheme == url.scheme && destination.host == url.host && destination.port == url.port
    }

    var checkRequest: URLRequest {
        var request = URLRequest(url: URL(string: "/api/workspace", relativeTo: url)!.absoluteURL,
                                 timeoutInterval: 1)
        request.setValue(token, forHTTPHeaderField: "X-ScreenDesk-Token")
        return request
    }
}

private final class LocalRequests: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

@MainActor
private final class ScreenDeskApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    private var window: NSWindow?
    private var browser: WKWebView?
    private var workspace: LocalWorkspace?
    private var backend: Process?
    private var connecting = false
    private lazy var localHTTP: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        return URLSession(configuration: config, delegate: LocalRequests(), delegateQueue: nil)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = menus()
        showWindow()
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        if let browser, let workspace {
            Task {
                let available = await isAvailable(workspace)
                if !available && browser === self.browser {
                    connect()
                }
            }
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Interrupt only the backend process this app created. The Python
        // launcher handles SIGINT by detaching its terminals and saving state.
        if let backend, backend.isRunning {
            kill(backend.processIdentifier, SIGINT)
        }
    }

    private func showWindow() {
        if let window {
            window.makeKeyAndOrderFront(nil)
            return
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1150, height: 740),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "ScreenDesk"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 620, height: 420)
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("ScreenDesk Workbench")
        self.window = window
        window.makeKeyAndOrderFront(nil)
        connect()
    }

    func windowWillClose(_ notification: Notification) {
        browser?.stopLoading()
        window?.contentView = nil
        browser = nil
        window = nil
    }

    private func showStatus(_ message: String, canRetry: Bool = false) {
        guard let window else { return }
        browser = nil
        let content = NSView()
        let label = NSTextField(wrappingLabelWithString: message)
        label.alignment = .center
        label.preferredMaxLayoutWidth = 500
        let stack = NSStackView(views: [label])
        stack.orientation = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        if canRetry {
            stack.addArrangedSubview(NSButton(title: "重试", target: self, action: #selector(retry)))
        }
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: content.widthAnchor, constant: -44)
        ])
        window.contentView = content
    }

    @objc private func retry() {
        showWindow()
        connect()
    }

    private func connect() {
        guard !connecting else { return }
        showStatus("正在打开 ScreenDesk 工作台…")
        connecting = true
        Task {
            defer { connecting = false }
            do {
                guard let configuration = Bundle.main.url(forResource: "checkout", withExtension: "txt") else {
                    showStatus("缺少 ScreenDesk 项目路径，请重新安装应用。")
                    return
                }
                let checkoutPath = try String(contentsOf: configuration,
                                              encoding: .utf8).trimmingCharacters(in: .newlines)
                let checkout = URL(fileURLWithPath: checkoutPath, isDirectory: true)
                let runtimeFile = checkout.appendingPathComponent("data/running.json")
                if let existing = await runningWorkspace(runtimeFile) {
                    open(existing)
                    return
                }
                if backend?.isRunning == true {
                    showStatus("ScreenDesk 后台暂时没有响应，请稍后重试。", canRetry: true)
                    return
                }
                let launcher = checkout.appendingPathComponent("start.sh")
                // Ask macOS for project folder access before spawning Python.
                try FileHandle(forReadingFrom: launcher).close()
                let errors = try temporaryLog()
                defer { try? errors.close() }
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/bin/sh")
                process.arguments = [launcher.path, "--no-browser"]
                var environment = ProcessInfo.processInfo.environment
                environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
                    + (environment["PATH"] ?? "")
                process.environment = environment
                process.standardInput = FileHandle.nullDevice
                process.standardOutput = FileHandle.nullDevice
                process.standardError = errors
                try process.run()
                backend = process
                while process.isRunning {
                    if let ready = await runningWorkspace(runtimeFile) {
                        open(ready)
                        return
                    }
                    try await Task.sleep(nanoseconds: 250_000_000)
                }
                // Another launcher may have acquired the backend lock first.
                if let existing = await runningWorkspace(runtimeFile) {
                    open(existing)
                    return
                }
                try errors.seek(toOffset: 0)
                let detail = String(decoding: try errors.read(upToCount: 4096) ?? Data(), as: UTF8.self)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                showStatus(detail.isEmpty ? "ScreenDesk 后台退出了，请重试。" : detail, canRetry: true)
            } catch {
                showStatus("无法打开 ScreenDesk 工作台。\n\(error.localizedDescription)\n\n"
                           + "如果项目在「文稿」中，请允许 ScreenDesk 访问该文件夹后重试。",
                           canRetry: true)
            }
        }
    }

    private func temporaryLog() throws -> FileHandle {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let fd = Darwin.open(url.path, O_CREAT | O_EXCL | O_RDWR, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { try? FileManager.default.removeItem(at: url) }
        return FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    }

    private func runningWorkspace(_ runtimeFile: URL) async -> LocalWorkspace? {
        guard let candidate = LocalWorkspace(runtimeFile: runtimeFile),
              await isAvailable(candidate) else { return nil }
        return candidate
    }

    private func isAvailable(_ candidate: LocalWorkspace) async -> Bool {
        guard let (_, response) = try? await localHTTP.data(for: candidate.checkRequest) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    private func open(_ workspace: LocalWorkspace) {
        guard let window else { return }
        self.workspace = workspace
        let browser = WKWebView()
        browser.navigationDelegate = self
        self.browser = browser
        window.contentView = browser
        browser.load(URLRequest(url: workspace.url))
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url, workspace?.contains(url) == true else {
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        guard webView === browser else { return }
        showStatus("无法加载本地工作台。\n\(error.localizedDescription)", canRetry: true)
    }

    private func menus() -> NSMenu {
        let menu = NSMenu()
        let app = NSMenuItem()
        app.submenu = NSMenu()
        app.submenu?.addItem(withTitle: "退出 ScreenDesk", action: #selector(NSApplication.terminate(_:)),
                             keyEquivalent: "q")
        menu.addItem(app)

        let edit = NSMenuItem()
        edit.submenu = NSMenu(title: "编辑")
        for (title, action, key) in [("剪切", "cut:", "x"), ("复制", "copy:", "c"),
                                     ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.submenu?.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        menu.addItem(edit)
        let window = NSMenuItem()
        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        window.submenu = windowMenu
        NSApp.windowsMenu = windowMenu
        menu.addItem(window)
        return menu
    }
}

@main
private enum DesktopMain {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = ScreenDeskApp()
        application.setActivationPolicy(.regular)
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
