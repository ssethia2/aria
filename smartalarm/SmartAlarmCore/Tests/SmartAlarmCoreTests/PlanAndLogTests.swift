import XCTest
@testable import SmartAlarmCore

final class WakePlanTests: XCTestCase {
    var calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        return calendar
    }()

    func date(_ day: Int, _ hour: Int, _ minute: Int) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 11, day: day, hour: hour, minute: minute))!
    }

    func testArmingAtNightTargetsTomorrowMorning() {
        let plan = WakePlan.next(hour: 7, minute: 0, windowMinutes: 60, after: date(8, 23, 15), calendar: calendar)
        XCTAssertEqual(plan.deadline, date(9, 7, 0))
        XCTAssertEqual(plan.windowStart, date(9, 6, 0))
    }

    func testArmingBeforeDawnTargetsThisMorning() {
        let plan = WakePlan.next(hour: 7, minute: 30, windowMinutes: 90, after: date(9, 2, 0), calendar: calendar)
        XCTAssertEqual(plan.deadline, date(9, 7, 30))
    }

    func testArmingExactlyAtTheTimeTargetsTomorrow() {
        let plan = WakePlan.next(hour: 7, minute: 0, windowMinutes: 60, after: date(9, 7, 0), calendar: calendar)
        XCTAssertEqual(plan.deadline, date(10, 7, 0))
    }

    func testAlarmSessionCoversTheTailWithGraceToRingPastTheDeadline() {
        let plan = WakePlan(deadline: date(9, 7, 0), windowMinutes: 90)
        XCTAssertEqual(plan.alarmSessionStart, date(9, 6, 35))
        // Apple's 30-minute session then runs to 07:05, still alive to ring at 07:00.
        XCTAssertEqual(plan.alarmSessionStart.addingTimeInterval(WakePlan.alarmSessionLength), date(9, 7, 5))
    }

    func testShortWindowStartsTheSessionWithTheWindow() {
        let plan = WakePlan(deadline: date(9, 7, 0), windowMinutes: 20)
        XCTAssertEqual(plan.alarmSessionStart, date(9, 6, 40))
    }

    func testHapticRampStartsGentle() {
        XCTAssertEqual(HapticRamp.step(0).strength, .gentle)
        XCTAssertEqual(HapticRamp.step(HapticRamp.gentlePulses).strength, .firm)
        XCTAssertLessThan(HapticRamp.step(HapticRamp.gentlePulses).interval, HapticRamp.step(0).interval)
    }
}

final class SleepStageTests: XCTestCase {
    let t0 = Date(timeIntervalSinceReferenceDate: 800_000_000)
    func at(_ minutes: Double) -> Date { t0.addingTimeInterval(minutes * 60) }

    lazy var night: [StageInterval] = [
        StageInterval(start: at(-480), end: at(30), stage: .inBed),
        StageInterval(start: at(-20), end: at(0), stage: .deep),
        StageInterval(start: at(0), end: at(15), stage: .core),
        StageInterval(start: at(15), end: at(25), stage: .rem),
        StageInterval(start: at(25), end: at(27), stage: .awake),
    ]

    func testSpecificStageBeatsInBed() {
        XCTAssertEqual(StageReport.stage(at: at(16), in: night), .rem)
        XCTAssertEqual(StageReport.stage(at: at(28), in: night), .inBed)
        XCTAssertNil(StageReport.stage(at: at(40), in: night))
    }

    func testMinutesAreClippedToTheRange() {
        let minutes = StageReport.minutes(in: DateInterval(start: at(-10), end: at(20)), from: night)
        XCTAssertEqual(minutes[.deep], 10)
        XCTAssertEqual(minutes[.core], 15)
        XCTAssertEqual(minutes[.rem], 5)
        XCTAssertNil(minutes[.inBed])
    }

    func testOverlappingSourcesAreNotDoubleCounted() {
        let doubled = night + [StageInterval(start: at(0), end: at(15), stage: .core)]
        let minutes = StageReport.minutes(in: DateInterval(start: at(0), end: at(15)), from: doubled)
        XCTAssertEqual(minutes[.core], 15)
    }

    func testInBedAloneIsNotStages() {
        XCTAssertFalse(StageReport.hasStages([night[0]]))
        XCTAssertTrue(StageReport.hasStages(night))
    }
}

final class NightStoreTests: XCTestCase {
    var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    func testRoundTripsAndListsNewestFirst() throws {
        let store = NightStore(directory: directory)
        let deadline = Date(timeIntervalSinceReferenceDate: 800_000_000)
        var older = NightLog(plan: WakePlan(deadline: deadline, windowMinutes: 60),
                             armedAt: deadline.addingTimeInterval(-86400 - 8 * 3600))
        older.notes = ["armed"]
        var newer = NightLog(plan: WakePlan(deadline: deadline.addingTimeInterval(86400), windowMinutes: 90),
                             armedAt: deadline.addingTimeInterval(-8 * 3600))
        newer.epochs = [Epoch(start: deadline, activity: 0.004, heartRate: 51)]
        newer.wake = WakeEvent(at: deadline.addingTimeInterval(86400 - 18 * 60), reason: .movement)
        try store.save(older)
        try store.save(newer)

        XCTAssertEqual(store.all().map(\.id), [newer.id, older.id])
        XCTAssertEqual(store.load(id: newer.id), newer)
        XCTAssertEqual(store.load(id: newer.id)?.minutesEarly, 18)
    }

    func testStageAtWakeComesFromAppleStages() {
        let deadline = Date(timeIntervalSinceReferenceDate: 800_000_000)
        var log = NightLog(plan: WakePlan(deadline: deadline, windowMinutes: 60), armedAt: deadline)
        log.wake = WakeEvent(at: deadline.addingTimeInterval(-600), reason: .heartRate)
        XCTAssertNil(log.stageAtWake)
        log.stages = [StageInterval(start: deadline.addingTimeInterval(-1200), end: deadline, stage: .rem)]
        XCTAssertEqual(log.stageAtWake, .rem)
        XCTAssertEqual(log.windowStageMinutes?[.rem], 20)
    }

    func testExportTextCarriesEverythingNeededForTuning() {
        let deadline = Date(timeIntervalSinceReferenceDate: 800_000_000)
        let armed = deadline.addingTimeInterval(-8 * 3600)
        var log = NightLog(plan: WakePlan(deadline: deadline, windowMinutes: 90), armedAt: armed)
        log.epochs = [Epoch(start: armed.addingTimeInterval(30), activity: 0.0031, heartRate: 52.04),
                      Epoch(start: armed.addingTimeInterval(60), activity: 0.02, heartRate: nil)]
        log.wake = WakeEvent(at: deadline.addingTimeInterval(-300), reason: .movement)
        log.batteryAtArm = 0.9
        log.stages = [StageInterval(start: armed, end: deadline, stage: .core)]
        log.notes = ["23:00:00 Apple's alarm session scheduled for 6:35 AM"]

        let lines = log.exportText.components(separatedBy: "\n")
        XCTAssertTrue(lines.contains { $0.hasSuffix("window 90min") })
        XCTAssertTrue(lines.contains { $0.hasPrefix("wake ") && $0.hasSuffix(" movement") })
        XCTAssertTrue(lines.contains("battery 0.90 ?"))
        XCTAssertTrue(lines.contains { $0.hasPrefix("note 23:00:00") })
        XCTAssertTrue(lines.contains { $0.hasPrefix("stage ") && $0.hasSuffix(" core") })
        XCTAssertEqual(lines.suffix(2), ["30,0.0031,52.0", "60,0.0200,"])
    }
}
