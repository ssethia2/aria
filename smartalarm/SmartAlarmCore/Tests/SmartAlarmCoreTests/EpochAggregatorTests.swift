import XCTest
@testable import SmartAlarmCore

final class EpochAggregatorTests: XCTestCase {
    // A whole multiple of 30 s, so epochs start exactly here.
    let t0 = Date(timeIntervalSinceReferenceDate: 800_000_010 - 800_000_010.truncatingRemainder(dividingBy: 30))

    func at(_ seconds: TimeInterval) -> Date { t0.addingTimeInterval(seconds) }

    func testAveragesMovementAndHeartRatePerEpoch() {
        var aggregator = EpochAggregator(length: 30)
        XCTAssertEqual(aggregator.addAcceleration(x: 0, y: 0, z: 1.0, at: at(1)), [])
        XCTAssertEqual(aggregator.addAcceleration(x: 0, y: 0, z: 1.1, at: at(2)), [])
        XCTAssertEqual(aggregator.addHeartRate(50, at: at(5)), [])
        XCTAssertEqual(aggregator.addHeartRate(54, at: at(25)), [])
        let closed = aggregator.addAcceleration(x: 0, y: 0, z: 1.0, at: at(31))
        XCTAssertEqual(closed.count, 1)
        XCTAssertEqual(closed[0].start, t0)
        XCTAssertEqual(closed[0].activity, 0.05, accuracy: 1e-9)
        XCTAssertEqual(closed[0].heartRate, 52)
    }

    func testMagnitudeIgnoresOrientation() {
        // Lying on your side: gravity on x instead of z is still 1 g, so zero movement.
        var aggregator = EpochAggregator(length: 30)
        _ = aggregator.addAcceleration(x: 1, y: 0, z: 0, at: at(1))
        let closed = aggregator.flush(upTo: at(30))
        XCTAssertEqual(closed.first?.activity ?? -1, 0, accuracy: 1e-9)
    }

    func testFlushClosesAQuietEpochOnlyOnceItEnds() {
        var aggregator = EpochAggregator(length: 30)
        _ = aggregator.addAcceleration(x: 0, y: 0, z: 1, at: at(1))
        XCTAssertEqual(aggregator.flush(upTo: at(29)), [])
        XCTAssertEqual(aggregator.flush(upTo: at(30)).count, 1)
        XCTAssertEqual(aggregator.flush(upTo: at(90)), [])
    }

    func testEpochWithoutMovementDataIsDropped() {
        var aggregator = EpochAggregator(length: 30)
        _ = aggregator.addHeartRate(50, at: at(1))
        XCTAssertEqual(aggregator.flush(upTo: at(31)), [])
    }

    func testGapSkipsEmptyEpochs() {
        var aggregator = EpochAggregator(length: 30)
        _ = aggregator.addAcceleration(x: 0, y: 0, z: 1, at: at(1))
        let closed = aggregator.addAcceleration(x: 0, y: 0, z: 1, at: at(155))
        XCTAssertEqual(closed.map(\.start), [t0])
        XCTAssertEqual(aggregator.flush(upTo: at(180)).map(\.start), [at(150)])
    }

    func testLateSampleFoldsIntoTheOpenEpoch() {
        var aggregator = EpochAggregator(length: 30)
        _ = aggregator.addAcceleration(x: 0, y: 0, z: 1, at: at(31))
        XCTAssertEqual(aggregator.addHeartRate(60, at: at(29)), [])
        XCTAssertEqual(aggregator.flush(upTo: at(60)).first?.heartRate, 60)
    }
}
