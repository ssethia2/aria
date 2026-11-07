import XCTest
@testable import SmartAlarmCore

final class WakeDetectorTests: XCTestCase {
    let detector = WakeDetector()
    // Deadline 07:00, 60-minute window from 06:00; armed at 23:00.
    let deadline = Date(timeIntervalSinceReferenceDate: 800_000_000)
    var plan: WakePlan { WakePlan(deadline: deadline, windowMinutes: 60) }
    var armed: Date { deadline.addingTimeInterval(-8 * 3600) }

    /// Still epochs every 30 s from `from` up to (not including) `to`.
    func night(from: Date, to: Date, activity: Double = 0.003, heartRate: Double? = 52) -> [Epoch] {
        stride(from: from.timeIntervalSinceReferenceDate, to: to.timeIntervalSinceReferenceDate, by: 30).map {
            Epoch(start: Date(timeIntervalSinceReferenceDate: $0), activity: activity, heartRate: heartRate)
        }
    }

    /// A still night up to `now`, with the last epochs replaced by `tail`.
    func night(until now: Date, tail: [(activity: Double, heartRate: Double?)]) -> [Epoch] {
        var epochs = night(from: armed, to: now)
        for (offset, sample) in tail.reversed().enumerated() {
            let index = epochs.count - 1 - offset
            epochs[index].activity = sample.activity
            epochs[index].heartRate = sample.heartRate
        }
        return epochs
    }

    func testStillNightWaitsForTheDeadline() {
        let now = plan.windowStart.addingTimeInterval(30 * 60)
        XCTAssertNil(detector.evaluate(night(from: armed, to: now), plan: plan, now: now))
        XCTAssertEqual(detector.evaluate(night(from: armed, to: deadline), plan: plan, now: deadline), .deadline)
    }

    func testMovementBurstInTheWindowWakes() {
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        let epochs = night(until: now, tail: [(0.04, 52), (0.05, 53)])
        XCTAssertEqual(detector.evaluate(epochs, plan: plan, now: now), .movement)
    }

    func testOneTwitchDoesNotWake() {
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        let epochs = night(until: now, tail: [(0.05, 52)])
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
    }

    func testMovementBeforeTheWindowIsIgnored() {
        let now = plan.windowStart.addingTimeInterval(-5 * 60)
        let epochs = night(until: now, tail: [(0.05, 52), (0.05, 52), (0.05, 52)])
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
    }

    func testBurstJustBeforeTheWindowOpensDoesNotCount() {
        // Moved at 05:59, still since: at 06:00:30 only in-window epochs may trigger.
        var epochs = night(from: armed, to: plan.windowStart.addingTimeInterval(60))
        let i = epochs.firstIndex { $0.start == plan.windowStart.addingTimeInterval(-60) }!
        epochs[i].activity = 0.05
        epochs[i + 1].activity = 0.05
        let now = plan.windowStart.addingTimeInterval(60)
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
    }

    func testRestlessSleeperNeedsABiggerBurst() {
        // A restless baseline (0.01 g) raises the bar to 0.04 g, so 0.03 g isn't a burst.
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        var epochs = night(from: armed, to: now, activity: 0.01)
        epochs[epochs.count - 1].activity = 0.03
        epochs[epochs.count - 2].activity = 0.03
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
        epochs[epochs.count - 1].activity = 0.045
        epochs[epochs.count - 2].activity = 0.045
        XCTAssertEqual(detector.evaluate(epochs, plan: plan, now: now), .movement)
    }

    func testHeartRateRiseWhileStillWakes() {
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        let epochs = night(until: now, tail: [(0.003, 59), (0.003, 60), (0.003, 59), (0.003, 60)])
        XCTAssertEqual(detector.evaluate(epochs, plan: plan, now: now), .heartRate)
    }

    func testSmallHeartRateRiseDoesNotWake() {
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        let epochs = night(until: now, tail: [(0.003, 55), (0.003, 56), (0.003, 55), (0.003, 56)])
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
    }

    func testHeartRateGapDoesNotTrigger() {
        // Three high readings and one missing: not enough to call it.
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        let epochs = night(until: now, tail: [(0.003, 60), (0.003, nil), (0.003, 60), (0.003, 60)])
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
    }

    func testSustainedMovementMeansAwake() {
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        let epochs = night(until: now, tail: [(0.1, 70), (0.12, 72), (0.09, 71)])
        XCTAssertEqual(detector.evaluate(epochs, plan: plan, now: now), .awake)
    }

    func testStaleBurstDoesNotTrigger() {
        // Moved at 06:10, then the sensors went quiet: at 06:20 that's old news.
        let burstAt = plan.windowStart.addingTimeInterval(10 * 60)
        var epochs = night(from: armed, to: burstAt)
        epochs.append(Epoch(start: burstAt, activity: 0.05, heartRate: 52))
        epochs.append(Epoch(start: burstAt.addingTimeInterval(30), activity: 0.05, heartRate: 52))
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
    }

    func testArmedLateFallsBackToTheFloor() {
        // Armed at 06:30: no pre-window baseline and no HR baseline, so movement uses the
        // 0.015 g floor and a heart-rate rise can't fire.
        let now = plan.windowStart.addingTimeInterval(32 * 60)
        var epochs = night(from: plan.windowStart.addingTimeInterval(30 * 60), to: now)
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
        epochs[epochs.count - 1].activity = 0.02
        epochs[epochs.count - 2].activity = 0.02
        XCTAssertEqual(detector.evaluate(epochs, plan: plan, now: now), .movement)
    }

    func testBaselineIsMedianActivityAndLowQuartileHeartRate() {
        var epochs = night(from: plan.windowStart.addingTimeInterval(-60 * 60), to: plan.windowStart)
        for i in epochs.indices {
            epochs[i].activity = i % 2 == 0 ? 0.002 : 0.004
            epochs[i].heartRate = Double(50 + i % 8)
        }
        let baseline = detector.baseline(from: epochs, windowStart: plan.windowStart, now: plan.windowStart)
        XCTAssertEqual(baseline.activity!, 0.003, accuracy: 0.0011)
        XCTAssertEqual(baseline.heartRate!, 51.75, accuracy: 0.5)
    }

    func testNoHeartRateAtAllStillDetectsMovement() {
        let now = plan.windowStart.addingTimeInterval(20 * 60)
        var epochs = night(from: armed, to: now, heartRate: nil)
        XCTAssertNil(detector.evaluate(epochs, plan: plan, now: now))
        epochs[epochs.count - 1].activity = 0.05
        epochs[epochs.count - 2].activity = 0.05
        XCTAssertEqual(detector.evaluate(epochs, plan: plan, now: now), .movement)
    }

    func testPercentile() {
        XCTAssertNil(percentile([], 0.5))
        XCTAssertEqual(percentile([3], 0.25), 3)
        XCTAssertEqual(percentile([1, 2, 3, 4], 0.5), 2.5)
        XCTAssertEqual(percentile([4, 1, 3, 2], 0.25)!, 1.75, accuracy: 1e-9)
    }
}
