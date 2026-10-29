// Cutter - casca nativa: sobe o servidor Python (app.py) e mostra a pagina numa janela propria.
// Build: mac/build.sh  (ou mac/build.sh --install)

import AppKit
import UniformTypeIdentifiers
@preconcurrency import WebKit

// app.py e index.html sao copiados para Contents/Resources pelo build.sh.
// CUTTER_DIR permite rodar direto da pasta do codigo durante o desenvolvimento.
let cutterDir: URL = {
    if let dev = ProcessInfo.processInfo.environment["CUTTER_DIR"] {
        return URL(fileURLWithPath: dev)
    }
    return Bundle.main.resourceURL!
}()

// Faixa invisivel no topo pra arrastar a janela (a pagina ocupa a janela toda, sob a barra de titulo).
final class DragStrip: NSView {
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { window?.performZoom(nil) } else { window?.performDrag(with: event) }
    }
}

// Arquivo arrastado pra janela: em vez do WKWebView abrir o arquivo, manda o caminho pra pagina.
final class FileDropWebView: WKWebView {
    var onFiles: (([URL]) -> Void)?
    var onHover: ((Bool) -> Void)?

    private func fileURLs(_ info: NSDraggingInfo) -> [URL] {
        let opts: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        return info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: opts) as? [URL] ?? []
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        if fileURLs(sender).isEmpty { return super.draggingEntered(sender) }
        onHover?(true)
        return .copy
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        fileURLs(sender).isEmpty ? super.draggingUpdated(sender) : .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        onHover?(false)
        super.draggingExited(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = fileURLs(sender)
        if urls.isEmpty { return super.performDragOperation(sender) }
        onHover?(false)
        onFiles?(urls)
        return true
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: FileDropWebView!
    var server: Process?
    var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        startServer()
    }

    // MARK: - Servidor Python

    func startServer() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        proc.arguments = ["python3", "app.py"]
        proc.currentDirectoryURL = cutterDir

        var env = ProcessInfo.processInfo.environment
        // yt-dlp, ffmpeg e deno vem do Homebrew; apps abertos pelo Finder nao herdam o PATH do shell.
        env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe

        // O app.py imprime "Cutter rodando em http://localhost:PORTA" ao subir.
        var buffer = ""
        var loaded = false
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let chunk = String(data: data, encoding: .utf8) else {
                handle.readabilityHandler = nil
                return
            }
            if loaded { return }
            buffer += chunk
            if let range = buffer.range(of: #"http://localhost:\d+"#, options: .regularExpression),
               let url = URL(string: String(buffer[range])) {
                loaded = true
                DispatchQueue.main.async { self?.webView.load(URLRequest(url: url)) }
            }
        }

        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.quitting || p.terminationStatus == 0 {
                    NSApp.terminate(nil)  // "Encerrar" na pagina tambem fecha o app
                } else {
                    self.showFatal("O servidor parou (codigo \(p.terminationStatus)).\n\n\(buffer.suffix(800))")
                }
            }
        }

        do {
            try proc.run()
            server = proc
        } catch {
            showFatal("Nao consegui iniciar o python3 (\(error.localizedDescription)).\n\nInstale com: xcode-select --install  ou  brew install python")
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let proc = server, proc.isRunning else { return .terminateNow }
        quitting = true
        // SIGINT -> app.py salva gravacoes em andamento e sai.
        proc.interrupt()
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(60)
            while proc.isRunning && Date() < deadline { usleep(100_000) }
            if proc.isRunning { proc.terminate() }
            DispatchQueue.main.async { sender.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // MARK: - Janela

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()  // mantem as preferencias (localStorage) entre aberturas
        config.userContentController.add(self, name: "cutter")  // window.webkit.messageHandlers.cutter

        webView = FileDropWebView(frame: .zero, configuration: config)
        webView.onFiles = { [weak self] urls in self?.sendFiles(urls) }
        webView.onHover = { [weak self] on in
            self?.webView.evaluateJavaScript("window.cutterDragHover && window.cutterDragHover(\(on))")
        }
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // sem flash branco antes da pagina carregar

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 680, height: 920),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Cutter"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.055, green: 0.055, blue: 0.063, alpha: 1)
        window.minSize = NSSize(width: 460, height: 620)
        let container = NSView()
        webView.autoresizingMask = [.width, .height]
        container.addSubview(webView)
        let strip = DragStrip()
        strip.autoresizingMask = [.width, .minYMargin]
        container.addSubview(strip)
        window.contentView = container
        webView.frame = container.bounds
        strip.frame = NSRect(x: 0, y: container.bounds.height - 40, width: container.bounds.width, height: 40)
        window.setFrameAutosaveName("CutterMainWindow")
        if !window.setFrameUsingName("CutterMainWindow") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Arquivos locais (arrastar ou "Escolher arquivo") -> transcricao

    func sendFiles(_ urls: [URL]) {
        let paths = urls.filter { $0.isFileURL }.map { $0.path }
        guard !paths.isEmpty,
              let data = try? JSONSerialization.data(withJSONObject: paths),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.cutterFiles && window.cutterFiles(\(json))")
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], body["action"] as? String == "pickFiles" else { return }
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.movie, .audio, .mpeg4Movie, .quickTimeMovie]
        panel.message = "Escolha videos ou audios para gerar a legenda (.txt e .srt)"
        panel.prompt = "Transcrever"
        panel.beginSheetModal(for: window) { [weak self] response in
            if response == .OK { self?.sendFiles(panel.urls) }
        }
    }

    func showFatal(_ text: String) {
        let alert = NSAlert()
        alert.messageText = "Cutter"
        alert.informativeText = text
        alert.alertStyle = .critical
        alert.runModal()
        NSApp.terminate(nil)
    }

    // MARK: - confirm()/alert() da pagina (WKWebView ignora sem isso)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancelar")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    // Links externos abrem no navegador padrao
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, url.host != "localhost", action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // MARK: - Menu (necessario pro Cmd+C / Cmd+V funcionarem no campo de link)

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Sobre o Cutter", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Ocultar Cutter", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Encerrar Cutter", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Editar")
        edit.addItem(withTitle: "Desfazer", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Refazer", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Recortar", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copiar", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Colar", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Selecionar Tudo", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let windowItem = NSMenuItem()
        let win = NSMenu(title: "Janela")
        let reload = NSMenuItem(title: "Recarregar", action: #selector(WKWebView.reload(_:)), keyEquivalent: "r")
        win.addItem(reload)
        win.addItem(.separator())
        win.addItem(withTitle: "Minimizar", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Fechar", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.submenu = win
        main.addItem(windowItem)
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
