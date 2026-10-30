// RecoFix.app — native macOS shell around the RecoFix engine.
//
// Starts the Python engine (with administrator rights so it can read /dev/rdisk*),
// waits for it on 127.0.0.1 and shows the UI in a WKWebView. Quitting the app
// stops the engine through its authenticated /api/quit endpoint.

import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    let port = 8765 + Int.random(in: 0..<200)
    let token = UUID().uuidString.replacingOccurrences(of: "-", with: "")
    var privileged = false
    var localEngine: Process?

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "RecoFix"
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 980, height: 620)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("RecoFixMain")
        window.backgroundColor = NSColor(red: 0.06, green: 0.067, blue: 0.082, alpha: 1)
        window.makeKeyAndOrderFront(nil)
        web.loadHTMLString(Self.splash, baseURL: nil)
        DispatchQueue.global().async { self.startEngine() }
    }

    // MARK: engine

    var resources: String { Bundle.main.resourcePath! }

    func python() -> String {
        for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return "/usr/bin/python3"
    }

    func shellQuote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    func startEngine() {
        let user = NSUserName()
        let uid = getuid(), gid = getgid()
        let logDir = NSHomeDirectory() + "/Library/Logs/RecoFix"
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        let cmd = "cd \(shellQuote(resources)) && env RECOFIX_TOKEN=\(token) SUDO_USER=\(shellQuote(user)) SUDO_UID=\(uid) " +
            "SUDO_GID=\(gid) HOME=\(shellQuote(NSHomeDirectory())) PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin " +
            "\(python()) -m recofix serve --port \(port) --no-browser > \(shellQuote(logDir + "/engine.log")) 2>&1 &"
        let script = "do shell script \"\(cmd.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\""))\" " +
            "with prompt \"RecoFix necesita permisos de administrador para leer los discos en modo de solo lectura.\" with administrator privileges"
        var err: NSDictionary? = ["skipped": true]
        if ProcessInfo.processInfo.environment["RECOFIX_NO_ADMIN"] == nil {
            err = nil
            DispatchQueue.main.sync { _ = NSAppleScript(source: script)?.executeAndReturnError(&err) }
        }
        if err == nil {
            privileged = true
        } else {
            // user declined: run unprivileged (disk images, folders, repair still work)
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python())
            p.arguments = ["-m", "recofix", "serve", "--port", "\(port)", "--no-browser"]
            p.currentDirectoryURL = URL(fileURLWithPath: resources)
            var env = ProcessInfo.processInfo.environment
            env["RECOFIX_TOKEN"] = token
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
            p.environment = env
            try? p.run()
            localEngine = p
        }
        waitForEngine(tries: 0)
    }

    func waitForEngine(tries: Int) {
        let url = URL(string: "http://127.0.0.1:\(port)/")!
        URLSession.shared.dataTask(with: url) { _, resp, _ in
            if (resp as? HTTPURLResponse) != nil {
                DispatchQueue.main.async {
                    self.web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/?t=\(self.token)")!))
                }
            } else if tries < 150 {
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.2) { self.waitForEngine(tries: tries + 1) }
            } else {
                DispatchQueue.main.async {
                    self.web.loadHTMLString(Self.splash.replacingOccurrences(of: "Iniciando motor…",
                        with: "No se pudo iniciar el motor. Revisa ~/Library/Logs/RecoFix/engine.log (¿Python 3 instalado?)"), baseURL: nil)
                }
            }
        }.resume()
    }

    func stopEngine() {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/quit")!)
        req.httpMethod = "POST"
        req.setValue(token, forHTTPHeaderField: "X-Token")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = "{}".data(using: .utf8)
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 2)
        localEngine?.terminate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ note: Notification) { stopEngine() }

    // MARK: web view

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = action.request.url, let host = u.host, host != "127.0.0.1", u.scheme?.hasPrefix("http") == true {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "Aceptar"); a.addButton(withTitle: "Cancelar")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "Aceptar"); a.addButton(withTitle: "Cancelar")
        a.window.initialFirstResponder = field
        completionHandler(a.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    // MARK: menu

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let app = NSMenu()
        app.addItem(withTitle: "Acerca de RecoFix", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Ocultar RecoFix", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Salir de RecoFix", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = app
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edición")
        edit.addItem(withTitle: "Deshacer", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cortar", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copiar", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Pegar", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Seleccionar todo", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "Visualización")
        view.addItem(withTitle: "Recargar", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = view
        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Ventana")
        win.addItem(withTitle: "Minimizar", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Cerrar", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.mainMenu = main
    }

    @objc func reload() { web.reload() }

    static let splash = """
    <html><body style="margin:0;height:100vh;display:grid;place-items:center;background:#0f1115;color:#8e96a8;
    font:14px -apple-system,sans-serif"><div style="text-align:center"><div style="font-size:22px;color:#e8ebf1;
    font-weight:600;margin-bottom:8px">RecoFix</div>Iniciando motor…</div></body></html>
    """
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
