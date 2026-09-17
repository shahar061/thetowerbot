// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "TowerInputAgent", platforms: [.macOS(.v14)], targets: [.executableTarget(name: "TowerInputAgent", path: "Sources/TowerInputAgent")])
