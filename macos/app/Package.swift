// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Tether",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "Tether", path: "Sources/Tether"),
    ]
)
