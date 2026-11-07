import Foundation

/// One fixed slice of the night: how much the wrist moved and the mean heart rate.
public struct Epoch: Codable, Equatable, Sendable {
    public var start: Date
    /// Mean |‖acceleration‖ − 1 g| over the slice, in g. Around 0.003 lying still.
    public var activity: Double
    /// Mean heart rate in bpm; nil when the sensor reported nothing in this slice.
    public var heartRate: Double?

    public init(start: Date, activity: Double, heartRate: Double? = nil) {
        self.start = start
        self.activity = activity
        self.heartRate = heartRate
    }
}

/// Folds raw accelerometer and heart-rate samples into epochs on a fixed grid
/// (boundaries at whole multiples of `length` since the reference date).
public struct EpochAggregator {
    public let length: TimeInterval
    private var currentStart: Date?
    private var activitySum = 0.0
    private var activityCount = 0
    private var heartRateSum = 0.0
    private var heartRateCount = 0

    public init(length: TimeInterval = 30) {
        self.length = length
    }

    /// Adds an accelerometer sample (x, y, z in g). Returns the epoch it closed, if any.
    public mutating func addAcceleration(x: Double, y: Double, z: Double, at date: Date) -> [Epoch] {
        let closed = advance(to: date)
        let magnitude = (x * x + y * y + z * z).squareRoot()
        activitySum += abs(magnitude - 1)
        activityCount += 1
        return closed
    }

    /// Adds a heart-rate reading. Returns the epoch it closed, if any.
    public mutating func addHeartRate(_ bpm: Double, at date: Date) -> [Epoch] {
        let closed = advance(to: date)
        heartRateSum += bpm
        heartRateCount += 1
        return closed
    }

    /// Closes the open epoch once `date` is past its end, so a quiet slice still lands on time.
    public mutating func flush(upTo date: Date) -> [Epoch] {
        guard let start = currentStart, date >= start.addingTimeInterval(length) else { return [] }
        return close()
    }

    private func slot(for date: Date) -> Date {
        let t = date.timeIntervalSinceReferenceDate
        return Date(timeIntervalSinceReferenceDate: (t / length).rounded(.down) * length)
    }

    private mutating func advance(to date: Date) -> [Epoch] {
        let target = slot(for: date)
        guard let start = currentStart else {
            currentStart = target
            return []
        }
        // Late samples fold into the open epoch rather than reopening a closed one.
        guard target > start else { return [] }
        let closed = close()
        currentStart = target
        return closed
    }

    private mutating func close() -> [Epoch] {
        defer {
            currentStart = nil
            activitySum = 0
            activityCount = 0
            heartRateSum = 0
            heartRateCount = 0
        }
        // A slice with no movement data says nothing about stillness, so it's dropped.
        guard let start = currentStart, activityCount > 0 else { return [] }
        let heartRate = heartRateCount > 0 ? heartRateSum / Double(heartRateCount) : nil
        return [Epoch(start: start, activity: activitySum / Double(activityCount), heartRate: heartRate)]
    }
}
