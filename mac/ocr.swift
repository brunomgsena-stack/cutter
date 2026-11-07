// cutter-ocr <imagem1> <imagem2> ... : le o texto de cada imagem com o Vision da Apple (offline)
// e imprime, para cada uma, um marcador \x01OCR\x01<caminho> seguido das linhas reconhecidas.
// O app.py usa isso para "ler a tela" de videos sem fala (slides, tutoriais).
import Foundation
import Vision
import AppKit

let paths = Array(CommandLine.arguments.dropFirst())
for path in paths {
    print("\u{1}OCR\u{1}\(path)")
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    req.recognitionLanguages = ["pt-BR", "en-US"]
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([req])
    for obs in (req.results ?? []) {
        if let top = obs.topCandidates(1).first { print(top.string) }
    }
}
