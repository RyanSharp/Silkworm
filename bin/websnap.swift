// websnap URL OUT.png [WIDTH] [HEIGHT] [WAIT_S]
// The page as desktop Safari's engine draws it: an offscreen WKWebView of the
// given size, snapshotted after WAIT_S seconds. Used by bin/dashshots for the
// laptop width; compiled on demand into ~/.cache/silkworm-dashshots/.
import AppKit
import WebKit

let args = CommandLine.arguments
guard args.count >= 3, let url = URL(string: args[1]) else {
  FileHandle.standardError.write("usage: websnap URL OUT.png [WIDTH] [HEIGHT] [WAIT_S]\n".data(using: .utf8)!)
  exit(2)
}
let out = args[2]
let width = CGFloat(Double(args.count > 3 ? args[3] : "1280") ?? 1280)
let height = CGFloat(Double(args.count > 4 ? args[4] : "860") ?? 860)
let wait = Double(args.count > 5 ? args[5] : "4") ?? 4

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let view = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: height))
let window = NSWindow(contentRect: view.frame, styleMask: [.borderless], backing: .buffered, defer: false)
window.appearance = NSAppearance(named: .darkAqua)
window.contentView = view
view.load(URLRequest(url: url))
DispatchQueue.main.asyncAfter(deadline: .now() + wait) {
  view.takeSnapshot(with: WKSnapshotConfiguration()) { image, error in
    guard let image = image, let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else {
      print("snapshot failed: \(String(describing: error))")
      exit(1)
    }
    do { try png.write(to: URL(fileURLWithPath: out)) } catch { print("\(error)"); exit(1) }
    exit(0)
  }
}
app.run()
