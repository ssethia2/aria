import Foundation

public enum WakeReason: String, Codable, Sendable {
    /// Sustained movement: you're already up.
    case awake
    /// A burst of movement well above your still baseline, the usual sign of a light-sleep arousal.
    case movement
    /// Heart rate climbed above your sleeping baseline while you lay still, typical of REM.
    case heartRate
    /// Nothing better came up; the latest wake time arrived.
    case deadline
}

/// Thresholds for the detector. The defaults are starting guesses, to be tuned
/// against Apple's next-morning sleep stages once there are a few nights of logs.
public struct DetectorConfig: Codable, Equatable, Sendable {
    public var epochLength: TimeInterval = 30
    /// Movement counts as a burst above baseline × this…
    public var movementMultiplier = 4.0
    /// …and above this floor (g), so a dead-still baseline doesn't turn a twitch into a burst.
    public var movementFloor = 0.015
    /// A burst needs this many of the last `burstLookback` epochs.
    public var burstEpochs = 2
    public var burstLookback = 3
    /// Heart rate must average this far above the sleeping baseline…
    public var heartRateRise = 6.0
    /// …over this many consecutive epochs.
    public var heartRateEpochs = 4
    /// Activity above this (g) for `awakeEpochs` epochs in a row means you're up.
    public var awakeActivity = 0.06
    public var awakeEpochs = 3
    /// How far before the window the baseline looks.
    public var baselineMinutes = 60.0
    /// With fewer baseline epochs than this, the movement trigger uses only the floor
    /// and the heart-rate trigger is off.
    public var minBaselineEpochs = 10

    public init() {}
}

/// What "asleep and still" looks like tonight.
public struct Baseline: Equatable, Sendable {
    /// Median activity: the typical still level.
    public var activity: Double?
    /// 25th-percentile heart rate: closer to the deep-sleep resting rate than the mean.
    public var heartRate: Double?
}

/// Decides, from the night's epochs, whether now is a good moment to wake you.
///
/// Apple doesn't expose live sleep stages, so this reads the two signals it can see.
/// Deep sleep is still with a low, steady heart rate; leaving it shows up as movement
/// (an arousal into light sleep) or a heart-rate rise with no movement (REM). Either
/// is a better moment to wake than deep sleep, so either fires.
public struct WakeDetector {
    public var config: DetectorConfig

    public init(config: DetectorConfig = DetectorConfig()) {
        self.config = config
    }

    public func baseline(from epochs: [Epoch], windowStart: Date, now: Date) -> Baseline {
        let from = windowStart.addingTimeInterval(-config.baselineMinutes * 60)
        var sample = epochs.filter { $0.start >= from && $0.start < windowStart }
        if sample.count < config.minBaselineEpochs {
            // Armed late, or the sensors were paused: fall back to everything except
            // the stretch the triggers are looking at right now.
            let recent = now.addingTimeInterval(-Double(lookback + 1) * config.epochLength)
            sample = epochs.filter { $0.start < recent }
        }
        guard sample.count >= config.minBaselineEpochs else { return Baseline() }
        let heartRates = sample.compactMap(\.heartRate)
        return Baseline(
            activity: percentile(sample.map(\.activity), 0.5),
            heartRate: heartRates.count >= config.minBaselineEpochs ? percentile(heartRates, 0.25) : nil
        )
    }

    public func evaluate(_ epochs: [Epoch], plan: WakePlan, now: Date) -> WakeReason? {
        if now >= plan.deadline { return .deadline }
        guard now >= plan.windowStart else { return nil }

        // Only fresh, in-window epochs can trigger: stale ones mean the sensors stopped.
        let freshFrom = max(plan.windowStart, now.addingTimeInterval(-Double(lookback + 1) * config.epochLength))
        let recent = epochs.filter { $0.start >= freshFrom }.sorted { $0.start < $1.start }
        guard !recent.isEmpty else { return nil }

        let awake = recent.suffix(config.awakeEpochs)
        if awake.count == config.awakeEpochs, awake.allSatisfy({ $0.activity >= config.awakeActivity }) {
            return .awake
        }

        let baseline = baseline(from: epochs, windowStart: plan.windowStart, now: now)
        let threshold = max(config.movementFloor, (baseline.activity ?? 0) * config.movementMultiplier)
        let bursts = recent.suffix(config.burstLookback).filter { $0.activity >= threshold }
        if bursts.count >= config.burstEpochs { return .movement }

        if let resting = baseline.heartRate {
            let rates = recent.suffix(config.heartRateEpochs).compactMap(\.heartRate)
            if rates.count == config.heartRateEpochs,
               rates.reduce(0, +) / Double(rates.count) >= resting + config.heartRateRise {
                return .heartRate
            }
        }
        return nil
    }

    private var lookback: Int {
        max(config.burstLookback, config.heartRateEpochs, config.awakeEpochs)
    }
}

func percentile(_ values: [Double], _ p: Double) -> Double? {
    guard !values.isEmpty else { return nil }
    let sorted = values.sorted()
    let position = p * Double(sorted.count - 1)
    let lower = Int(position.rounded(.down))
    let upper = min(lower + 1, sorted.count - 1)
    let fraction = position - Double(lower)
    return sorted[lower] + (sorted[upper] - sorted[lower]) * fraction
}
