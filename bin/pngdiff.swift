// pngdiff A.png B.png [--skip-top N] [--skip-bottom N]
// Decodes two screenshots to RGBA and compares them pixel for pixel (the PNG
// bytes are not compared: two encoders differ on identical pixels). Prints
// one line: "same", or how many pixels differ and the box around them.
// --skip-top N ignores the top N rows (a simulator's status bar, whose clock
// moves between two shots taken seconds apart); --skip-bottom N the bottom N
// (Mobile Safari's toolbar, translucent over the page and drawn by Safari, not
// the page). Exit 0 same, 1 different,
// 2 unreadable or different sizes. Used to prove the phone's dashboard did not
// change: bin/dashshots --before ... --widths 375,430, then this on each pair.
import CoreGraphics
import Foundation
import ImageIO

func rgba(_ path: String) -> (Int, Int, [UInt8])? {
  guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
        let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return nil }
  let w = img.width, h = img.height
  var buf = [UInt8](repeating: 0, count: w * h * 4)
  let ok = buf.withUnsafeMutableBytes { raw -> Bool in
    guard let ctx = CGContext(data: raw.baseAddress, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: w * 4, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    return true
  }
  return ok ? (w, h, buf) : nil
}

let args = CommandLine.arguments
guard args.count >= 3 else {
  FileHandle.standardError.write("usage: pngdiff A.png B.png [--skip-top N] [--skip-bottom N]\n".data(using: .utf8)!)
  exit(2)
}
var skip = 0
if let i = args.firstIndex(of: "--skip-top"), i + 1 < args.count { skip = Int(args[i + 1]) ?? 0 }
var skipBottom = 0
if let i = args.firstIndex(of: "--skip-bottom"), i + 1 < args.count { skipBottom = Int(args[i + 1]) ?? 0 }
guard let a = rgba(args[1]), let b = rgba(args[2]) else { print("unreadable"); exit(2) }
guard a.0 == b.0, a.1 == b.1 else { print("sizes differ: \(a.0)x\(a.1) vs \(b.0)x\(b.1)"); exit(2) }
let (w, h) = (a.0, a.1)
var n = 0, x0 = w, y0 = h, x1 = -1, y1 = -1
// The bitmap's first row is the image's top row.
for y in skip..<max(skip, h - skipBottom) {
  let base = y * w * 4
  for x in 0..<w {
    let o = base + x * 4
    if a.2[o] != b.2[o] || a.2[o + 1] != b.2[o + 1] || a.2[o + 2] != b.2[o + 2] || a.2[o + 3] != b.2[o + 3] {
      n += 1; x0 = min(x0, x); x1 = max(x1, x); y0 = min(y0, y); y1 = max(y1, y)
    }
  }
}
if n == 0 { print("same"); exit(0) }
print("\(n) pixels differ, in x \(x0)-\(x1), y \(y0)-\(y1)")
exit(1)
