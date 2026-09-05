import Foundation
import ImageCaptureCore

private struct SourceRecord: Codable {
    let name: String
    let currentMode: String
    let supportedBitDepths: [Int]
    let resolutions: [Int]
    let preferredResolutions: [Int]
    let nativeXResolution: Int
    let nativeYResolution: Int
}

private struct ScannerRecord: Codable {
    let id: String
    let name: String
    let productKind: String?
    let transport: String?
    let location: String?
    let uuid: String?
    let persistentID: String?
    let serialNumber: String?
    let modulePath: String
    let moduleVersion: String?
    let sources: [SourceRecord]
    let inspectionError: String?
}

private struct DiscoveryResult: Codable {
    let version: Int
    let scanners: [ScannerRecord]
}

private final class ScannerDiscovery: NSObject, ICDeviceBrowserDelegate {
    private var scanners: [String: ICScannerDevice] = [:]

    var devices: [ICScannerDevice] {
        scanners.values.sorted {
            if $0.name == $1.name {
                return Self.identifier(for: $0) < Self.identifier(for: $1)
            }
            return ($0.name ?? "").localizedCaseInsensitiveCompare($1.name ?? "") == .orderedAscending
        }
    }

    func deviceBrowser(
        _ browser: ICDeviceBrowser,
        didAdd device: ICDevice,
        moreComing: Bool
    ) {
        guard let scanner = device as? ICScannerDevice else {
            return
        }
        scanners[Self.identifier(for: scanner)] = scanner
    }

    func deviceBrowser(
        _ browser: ICDeviceBrowser,
        didRemove device: ICDevice,
        moreGoing: Bool
    ) {
        guard let scanner = device as? ICScannerDevice else {
            return
        }
        scanners.removeValue(forKey: Self.identifier(for: scanner))
    }

    static func identifier(for scanner: ICScannerDevice) -> String {
        scanner.persistentIDString
            ?? scanner.uuidString
            ?? scanner.serialNumberString
            ?? scanner.name
            ?? "unknown-scanner"
    }

}

private final class ScannerInspector: NSObject, ICScannerDeviceDelegate {
    private(set) var openFinished = false
    private(set) var openError: Error?
    private(set) var selectionFinished = false
    private(set) var selectedUnit: ICScannerFunctionalUnit?
    private(set) var selectionError: Error?
    private(set) var closeFinished = false

    func device(_ device: ICDevice, didOpenSessionWithError error: Error?) {
        openError = error
        openFinished = true
    }

    func device(_ device: ICDevice, didCloseSessionWithError error: Error?) {
        closeFinished = true
    }

    func didRemove(_ device: ICDevice) {
        if !openFinished {
            openError = NSError(
                domain: "scanbox-native",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "scanner disappeared during inspection"]
            )
            openFinished = true
        }
    }

    func scannerDevice(
        _ scanner: ICScannerDevice,
        didSelect functionalUnit: ICScannerFunctionalUnit,
        error: Error?
    ) {
        selectedUnit = functionalUnit
        selectionError = error
        selectionFinished = true
    }

    func resetSelection() {
        selectionFinished = false
        selectedUnit = nil
        selectionError = nil
    }
}

private func runLoop(until condition: () -> Bool, timeout: TimeInterval) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while !condition() && Date() < deadline {
        RunLoop.current.run(mode: .default, before: min(deadline, Date().addingTimeInterval(0.05)))
    }
    return condition()
}

private func sourceName(_ type: ICScannerFunctionalUnitType) -> String {
    switch type {
    case .flatbed: return "flatbed"
    case .documentFeeder: return "feeder"
    case .positiveTransparency: return "positive-transparency"
    case .negativeTransparency: return "negative-transparency"
    @unknown default: return "unknown-\(type.rawValue)"
    }
}

private func modeName(_ type: ICScannerPixelDataType) -> String {
    switch type {
    case .BW: return "black-and-white"
    case .gray: return "grayscale"
    case .RGB: return "color-rgb"
    case .palette: return "palette"
    case .CMY: return "color-cmy"
    case .CMYK: return "color-cmyk"
    case .YUV: return "color-yuv"
    case .YUVK: return "color-yuvk"
    case .CIEXYZ: return "color-ciexyz"
    @unknown default: return "unknown-\(type.rawValue)"
    }
}

private func values(in set: IndexSet) -> [Int] {
    set.map { Int($0) }
}

private func inspect(_ scanner: ICScannerDevice, timeout: TimeInterval) -> ScannerRecord {
    let inspector = ScannerInspector()
    scanner.delegate = inspector
    scanner.requestOpenSession()

    var sources: [SourceRecord] = []
    var inspectionError: String?
    if !runLoop(until: { inspector.openFinished }, timeout: timeout) {
        inspectionError = "timed out opening scanner session"
    } else if let error = inspector.openError {
        inspectionError = "could not open scanner session: \(error.localizedDescription)"
    } else {
        let types = scanner.availableFunctionalUnitTypes.compactMap {
            ICScannerFunctionalUnitType(rawValue: UInt($0.intValue))
        }.sorted { $0.rawValue < $1.rawValue }

        for type in types {
            inspector.resetSelection()
            scanner.requestSelect(type)
            guard runLoop(until: { inspector.selectionFinished }, timeout: timeout) else {
                inspectionError = "timed out selecting \(sourceName(type))"
                break
            }
            if let error = inspector.selectionError {
                inspectionError = "could not select \(sourceName(type)): \(error.localizedDescription)"
                break
            }
            guard let unit = inspector.selectedUnit else {
                inspectionError = "scanner returned no capabilities for \(sourceName(type))"
                break
            }
            sources.append(SourceRecord(
                name: sourceName(type),
                currentMode: modeName(unit.pixelDataType),
                supportedBitDepths: values(in: unit.supportedBitDepths),
                resolutions: values(in: unit.supportedResolutions),
                preferredResolutions: values(in: unit.preferredResolutions),
                nativeXResolution: Int(unit.nativeXResolution),
                nativeYResolution: Int(unit.nativeYResolution)
            ))
        }

        scanner.requestCloseSession()
        _ = runLoop(until: { inspector.closeFinished }, timeout: timeout)
    }

    scanner.delegate = nil
    return ScannerRecord(
        id: ScannerDiscovery.identifier(for: scanner),
        name: scanner.name ?? "Unnamed scanner",
        productKind: scanner.productKind,
        transport: scanner.transportType,
        location: scanner.locationDescription,
        uuid: scanner.uuidString,
        persistentID: scanner.persistentIDString,
        serialNumber: scanner.serialNumberString,
        modulePath: scanner.modulePath,
        moduleVersion: scanner.moduleVersion,
        sources: sources,
        inspectionError: inspectionError
    )
}

private func usage() -> Never {
    FileHandle.standardError.write(Data(
        "usage: scanbox-native discover [--timeout SECONDS]\n".utf8
    ))
    exit(2)
}

private func parseTimeout(_ arguments: [String]) -> TimeInterval {
    var timeout: TimeInterval = 5
    var index = 0
    while index < arguments.count {
        guard arguments[index] == "--timeout", index + 1 < arguments.count,
              let value = TimeInterval(arguments[index + 1]), value > 0 else {
            usage()
        }
        timeout = value
        index += 2
    }
    return timeout
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.first == "discover" else {
    usage()
}
let timeout = parseTimeout(Array(arguments.dropFirst()))

private let discovery = ScannerDiscovery()
private let browser = ICDeviceBrowser()
browser.delegate = discovery
browser.browsedDeviceTypeMask = ICDeviceTypeMask(rawValue:
    ICDeviceTypeMask.scanner.rawValue
        | ICDeviceLocationTypeMask.local.rawValue
        | ICDeviceLocationTypeMask.shared.rawValue
        | ICDeviceLocationTypeMask.bonjour.rawValue
)!
browser.start()
RunLoop.current.run(until: Date().addingTimeInterval(timeout))
private let records = discovery.devices.map { inspect($0, timeout: timeout) }
browser.stop()

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
do {
    let data = try encoder.encode(DiscoveryResult(version: 1, scanners: records))
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    FileHandle.standardError.write(Data("scanbox-native: \(error)\n".utf8))
    exit(1)
}
