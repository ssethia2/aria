import Foundation

public struct WakeEvent: Codable, Equatable, Sendable {
    public var at: Date
    public var reason: WakeReason

    public init(at: Date, reason: WakeReason) {
        self.at = at
        self.reason = reason
    }
}

/// Everything recorded about one night, kept so the detector can be tuned against
/// what Apple says actually happened.
public struct NightLog: Codable, Equatable, Identifiable, Sendable {
    public var id: UUID
    public var plan: WakePlan
    public var armedAt: Date
    public var epochs: [Epoch] = []
    public var wake: WakeEvent?
    public var stoppedAt: Date?
    /// Apple's stages around the window, filled in once HealthKit has them.
    public var stages: [StageInterval]?
    /// Battery level (0–1) at arming and at stop: the overnight cost of the workout session.
    public var batteryAtArm: Double?
    public var batteryAtStop: Double?
    /// Timestamped events worth reading the next morning (sessions starting, failing, pausing).
    public var notes: [String] = []

    public init(id: UUID = UUID(), plan: WakePlan, armedAt: Date) {
        self.id = id
        self.plan = plan
        self.armedAt = armedAt
    }

    public var stageAtWake: SleepStage? {
        guard let wake, let stages else { return nil }
        return StageReport.stage(at: wake.at, in: stages)
    }

    public var minutesEarly: Int? {
        guard let wake else { return nil }
        return max(0, Int((plan.deadline.timeIntervalSince(wake.at) / 60).rounded()))
    }

    public var windowStageMinutes: [SleepStage: Double]? {
        guard let stages else { return nil }
        return StageReport.minutes(in: DateInterval(start: plan.windowStart, end: plan.deadline), from: stages)
    }

    /// The night as plain text, to share off the watch for tuning the detector.
    public var exportText: String {
        let iso = ISO8601DateFormatter()
        let battery = [batteryAtArm, batteryAtStop].map { $0.map { String(format: "%.2f", $0) } ?? "?" }
        var lines = [
            "smartalarm night \(id.uuidString)",
            "armed \(iso.string(from: armedAt)) deadline \(iso.string(from: plan.deadline)) window \(plan.windowMinutes)min",
            "wake " + (wake.map { "\(iso.string(from: $0.at)) \($0.reason.rawValue)" } ?? "none"),
            "battery \(battery[0]) \(battery[1])",
        ]
        lines += notes.map { "note \($0)" }
        lines += (stages ?? []).map { "stage \(iso.string(from: $0.start)) \(iso.string(from: $0.end)) \($0.stage.rawValue)" }
        lines.append("epochs seconds_after_armed,activity_g,heart_rate")
        lines += epochs.map { epoch in
            let heartRate = epoch.heartRate.map { String(format: "%.1f", $0) } ?? ""
            return "\(Int(epoch.start.timeIntervalSince(armedAt))),\(String(format: "%.4f", epoch.activity)),\(heartRate)"
        }
        return lines.joined(separator: "\n")
    }
}

/// One JSON file per night in `directory`.
public struct NightStore {
    public let directory: URL

    public init(directory: URL) {
        self.directory = directory
    }

    public func save(_ log: NightLog) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Self.encoder.encode(log).write(to: url(for: log.id), options: .atomic)
    }

    public func load(id: UUID) -> NightLog? {
        guard let data = try? Data(contentsOf: url(for: id)) else { return nil }
        return try? Self.decoder.decode(NightLog.self, from: data)
    }

    /// Every saved night, newest first.
    public func all() -> [NightLog] {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files
            .filter { $0.pathExtension == "json" }
            .compactMap { try? Self.decoder.decode(NightLog.self, from: Data(contentsOf: $0)) }
            .sorted { $0.armedAt > $1.armedAt }
    }

    private func url(for id: UUID) -> URL {
        directory.appendingPathComponent("\(id.uuidString).json")
    }

    private static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
}
