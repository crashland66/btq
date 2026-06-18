import Foundation

extension URL {
    var btqUsesHTTPS: Bool {
        scheme?.lowercased() == "https"
    }
}
