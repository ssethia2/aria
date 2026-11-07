import CoreMotion
import Foundation
import HealthKit
import SmartAlarmCore

enum HealthTypes {
    static let heartRate = HKQuantityType(.heartRate)
    static let sleep = HKCategoryType(.sleepAnalysis)
}

/// Keeps the app running through the night and streams wrist movement and heart rate.
///
/// Apple's smart-alarm session only lasts 30 minutes; a workout session is the only way
/// for a watch app to stay running longer, so the night runs inside one. The workout is
/// discarded at the end, so nothing lands in Fitness or your rings.
@MainActor
final class SensorMonitor: NSObject {
    let healthStore = HKHealthStore()
    var onAcceleration: (@MainActor (Double, Double, Double, Date) -> Void)?
    var onHeartRate: (@MainActor (Double, Date) -> Void)?
    var onNote: (@MainActor (String) -> Void)?

    private let motion = CMMotionManager()
    private var session: HKWorkoutSession?
    private var builder: HKLiveWorkoutBuilder?
    // CoreMotion stamps samples in seconds since boot.
    private let bootDate = Date(timeIntervalSinceNow: -ProcessInfo.processInfo.systemUptime)

    var isRunning: Bool { motion.isAccelerometerActive }

    func requestAuthorization() async throws {
        try await healthStore.requestAuthorization(toShare: [HKObjectType.workoutType()],
                                                   read: [HealthTypes.heartRate, HealthTypes.sleep])
    }

    func start() throws {
        let configuration = HKWorkoutConfiguration()
        configuration.activityType = .other
        configuration.locationType = .indoor
        let session = try HKWorkoutSession(healthStore: healthStore, configuration: configuration)
        attach(session, configuration: configuration)
        let start = Date()
        session.startActivity(with: start)
        builder?.beginCollection(withStart: start) { [weak self] _, error in
            guard let error else { return }
            Task { @MainActor in self?.onNote?("Heart-rate collection failed: \(error.localizedDescription)") }
        }
        startMotion()
    }

    /// Picks up a workout that outlived a crash of the app.
    func recover() async -> Bool {
        let session: HKWorkoutSession? = await withCheckedContinuation { continuation in
            healthStore.recoverActiveWorkoutSession { session, _ in continuation.resume(returning: session) }
        }
        guard let session else { return false }
        attach(session, configuration: session.workoutConfiguration)
        startMotion()
        return true
    }

    /// Movement without a workout: the fallback when Apple's alarm session relaunches the
    /// app in the background and a workout can't be started from there.
    func startMotion() {
        guard motion.isAccelerometerAvailable, !motion.isAccelerometerActive else { return }
        motion.accelerometerUpdateInterval = 0.1
        motion.startAccelerometerUpdates(to: .main) { [weak self] data, _ in
            guard let data else { return }
            MainActor.assumeIsolated {
                guard let self else { return }
                let a = data.acceleration
                self.onAcceleration?(a.x, a.y, a.z, self.bootDate.addingTimeInterval(data.timestamp))
            }
        }
    }

    /// Battery saver: the heart-rate sensor is what costs power, and pausing turns it off.
    func pauseHeartRate() { session?.pause() }
    func resumeHeartRate() { session?.resume() }

    func stop() {
        motion.stopAccelerometerUpdates()
        // Ending collection and discarding the workout happen once the session reports .ended.
        session?.end()
    }

    private func attach(_ session: HKWorkoutSession, configuration: HKWorkoutConfiguration) {
        let builder = session.associatedWorkoutBuilder()
        builder.dataSource = HKLiveWorkoutDataSource(healthStore: healthStore, workoutConfiguration: configuration)
        session.delegate = self
        builder.delegate = self
        self.session = session
        self.builder = builder
    }
}

extension SensorMonitor: HKWorkoutSessionDelegate {
    nonisolated func workoutSession(_ workoutSession: HKWorkoutSession, didChangeTo toState: HKWorkoutSessionState,
                                    from fromState: HKWorkoutSessionState, date: Date) {
        Task { @MainActor in
            switch toState {
            case .paused:
                self.onNote?("Heart rate off (battery saver)")
            case .running where fromState == .paused:
                self.onNote?("Heart rate back on")
            case .ended:
                guard let builder = self.builder, workoutSession === self.session else { return }
                self.session = nil
                self.builder = nil
                try? await builder.endCollection(at: date)
                builder.discardWorkout()
            default:
                break
            }
        }
    }

    nonisolated func workoutSession(_ workoutSession: HKWorkoutSession, didFailWithError error: Error) {
        Task { @MainActor in self.onNote?("Workout session failed: \(error.localizedDescription)") }
    }
}

extension SensorMonitor: HKLiveWorkoutBuilderDelegate {
    nonisolated func workoutBuilder(_ workoutBuilder: HKLiveWorkoutBuilder, didCollectDataOf collectedTypes: Set<HKSampleType>) {
        guard collectedTypes.contains(HealthTypes.heartRate),
              let statistics = workoutBuilder.statistics(for: HealthTypes.heartRate),
              let quantity = statistics.mostRecentQuantity() else { return }
        let bpm = quantity.doubleValue(for: HKUnit.count().unitDivided(by: .minute()))
        let date = statistics.mostRecentQuantityDateInterval()?.end ?? Date()
        Task { @MainActor in self.onHeartRate?(bpm, date) }
    }

    nonisolated func workoutBuilderDidCollectEvent(_ workoutBuilder: HKLiveWorkoutBuilder) {}
}

enum HealthStages {
    /// Apple's sleep samples between two dates. Stages appear only after the watch has
    /// processed the night, typically some time after you get up.
    static func fetch(from start: Date, to end: Date, store: HKHealthStore) async throws -> [StageInterval] {
        try await withCheckedThrowingContinuation { continuation in
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: [])
            let query = HKSampleQuery(sampleType: HealthTypes.sleep, predicate: predicate,
                                      limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, error in
                if let error {
                    continuation.resume(throwing: error)
                    return
                }
                let intervals = (samples as? [HKCategorySample] ?? []).compactMap { sample in
                    stage(for: sample.value).map { StageInterval(start: sample.startDate, end: sample.endDate, stage: $0) }
                }
                continuation.resume(returning: intervals)
            }
            store.execute(query)
        }
    }

    static func stage(for value: Int) -> SleepStage? {
        guard let value = HKCategoryValueSleepAnalysis(rawValue: value) else { return nil }
        switch value {
        case .inBed: return .inBed
        case .awake: return .awake
        case .asleepREM: return .rem
        case .asleepCore: return .core
        case .asleepDeep: return .deep
        case .asleepUnspecified: return .asleep
        @unknown default: return nil
        }
    }
}
