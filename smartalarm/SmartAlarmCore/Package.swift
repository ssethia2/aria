// swift-tools-version: 5.9
// The watch app's logic, kept free of WatchKit/HealthKit so it builds and tests on a Mac.
import PackageDescription

let package = Package(
    name: "SmartAlarmCore",
    platforms: [.watchOS(.v10), .macOS(.v13)],
    products: [
        .library(name: "SmartAlarmCore", targets: ["SmartAlarmCore"]),
    ],
    targets: [
        .target(name: "SmartAlarmCore"),
        .testTarget(name: "SmartAlarmCoreTests", dependencies: ["SmartAlarmCore"]),
    ]
)
