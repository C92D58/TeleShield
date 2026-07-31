// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "TeleShieldSwiftUI",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(
            name: "TeleShieldApp",
            targets: ["TeleShieldApp"]
        )
    ],
    dependencies: [
        .package(url: "https://github.com/attaswift/BigInt.git", from: "5.4.0")
    ],
    targets: [
        .executableTarget(
            name: "TeleShieldApp",
            dependencies: [
                .product(name: "BigInt", package: "BigInt")
            ],
            path: "Sources/TeleShieldApp"
        ),
        .testTarget(
            name: "TeleShieldAppTests",
            dependencies: ["TeleShieldApp"],
            path: "Tests/TeleShieldAppTests"
        )
    ]
)
