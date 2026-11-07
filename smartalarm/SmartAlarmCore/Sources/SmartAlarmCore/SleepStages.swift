import Foundation

/// Apple's sleep stages, as HealthKit reports them the morning after.
public enum SleepStage: String, Codable, CaseIterable, Sendable {
    case awake, rem, core, deep
    /// Asleep, stage unknown (older watches, or another app's data).
    case asleep
    case inBed

    /// When samples overlap (in-bed under the stages, two sources), the more specific one wins.
    var specificity: Int {
        switch self {
        case .awake, .rem, .core, .deep: return 2
        case .asleep: return 1
        case .inBed: return 0
        }
    }

    public var label: String {
        switch self {
        case .awake: return "Awake"
        case .rem: return "REM"
        case .core: return "Core"
        case .deep: return "Deep"
        case .asleep: return "Asleep"
        case .inBed: return "In bed"
        }
    }
}

public struct StageInterval: Codable, Equatable, Sendable {
    public var start: Date
    public var end: Date
    public var stage: SleepStage

    public init(start: Date, end: Date, stage: SleepStage) {
        self.start = start
        self.end = end
        self.stage = stage
    }
}

public enum StageReport {
    /// The stage Apple recorded at `date`, nil if it recorded nothing then.
    public static func stage(at date: Date, in intervals: [StageInterval]) -> SleepStage? {
        intervals
            .filter { $0.start <= date && date < $0.end }
            .max { $0.stage.specificity < $1.stage.specificity }?
            .stage
    }

    /// Minutes in each stage across `range`, sampled every 30 s so overlapping sources aren't double-counted.
    public static func minutes(in range: DateInterval, from intervals: [StageInterval]) -> [SleepStage: Double] {
        var totals: [SleepStage: Double] = [:]
        let step: TimeInterval = 30
        var t = range.start
        while t < range.end {
            if let stage = stage(at: t, in: intervals) {
                totals[stage, default: 0] += step / 60
            }
            t = t.addingTimeInterval(step)
        }
        return totals
    }

    /// True once Apple has written real stages (not just in-bed/asleep) for the night.
    public static func hasStages(_ intervals: [StageInterval]) -> Bool {
        intervals.contains { $0.stage.specificity == 2 }
    }
}
