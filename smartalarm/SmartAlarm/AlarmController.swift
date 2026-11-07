import Foundation
import SmartAlarmCore
import SwiftUI
import WatchKit

/// Runs one night: starts the sensors and Apple's alarm session, feeds the detector,
/// rings, and keeps the night's log.
///
/// The window is covered in two layers. The overnight workout keeps the app running, so
/// the detector watches the whole window and buzzes with the app's own haptics if it
/// fires early. Apple's smart-alarm session covers the last 25 minutes plus 5 of grace,
/// so the deadline rings with the system alarm screen even if the app died in the night.
@MainActor
final class AlarmController: NSObject, ObservableObject {
    static let shared = AlarmController()

    enum Phase: Equatable {
        case idle, armed, ringing(WakeReason)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var night: NightLog?
    @Published private(set) var history: [NightLog] = []
    @Published private(set) var heartRate: Double?
    @Published private(set) var moving = false
    @Published var error: String?

    private let sensors = SensorMonitor()
    private let store = NightStore(directory: FileManager.default
        .urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("nights"))
    private let detector = WakeDetector()
    private var aggregator = EpochAggregator()
    private var alarmSession: WKExtendedRuntimeSession?
    private var ringingViaSystem = false
    private var tickTimer: Timer?
    private var hapticTimer: Timer?
    private var hapticPulse = 0
    private var batterySaver = false
    private var heartRatePaused = false
    private var startingSensors = false
    private var unsavedEpochs = 0

    private static let activeNightKey = "activeNightID"
    private static let batterySaverKey = "activeNightBatterySaver"
    /// Battery saver turns heart rate back on this long before the window, for a baseline.
    private static let heartRateLead: TimeInterval = 30 * 60
    /// Give up ringing after this long with no response, to save the battery.
    private static let maxRinging: TimeInterval = 30 * 60

    override init() {
        super.init()
        WKInterfaceDevice.current().isBatteryMonitoringEnabled = true
        sensors.onAcceleration = { [weak self] x, y, z, date in
            guard let self else { return }
            self.append(self.aggregator.addAcceleration(x: x, y: y, z: z, at: date))
        }
        sensors.onHeartRate = { [weak self] bpm, date in
            guard let self else { return }
            self.heartRate = bpm
            self.append(self.aggregator.addHeartRate(bpm, at: date))
        }
        sensors.onNote = { [weak self] text in self?.note(text) }
        history = store.all()
        restoreActiveNight()
    }

    // MARK: Arming and stopping

    func arm(hour: Int, minute: Int, windowMinutes: Int, batterySaver: Bool) async {
        let plan = WakePlan.next(hour: hour, minute: minute, windowMinutes: windowMinutes, after: Date())
        await arm(plan, batterySaver: batterySaver)
    }

    /// A dry run before trusting it overnight: rings within 3 minutes, sooner if you move.
    func armTest() async {
        await arm(WakePlan(deadline: Date().addingTimeInterval(3 * 60), windowMinutes: 30), batterySaver: false)
        note("Test run")
    }

    private func arm(_ plan: WakePlan, batterySaver: Bool) async {
        error = nil
        do {
            try await sensors.requestAuthorization()
        } catch {
            self.error = "Health access failed: \(error.localizedDescription)"
            return
        }
        let now = Date()
        do {
            try sensors.start()
        } catch {
            self.error = "Couldn't start the overnight session: \(error.localizedDescription)"
            return
        }
        var night = NightLog(plan: plan, armedAt: now)
        night.batteryAtArm = batteryLevel
        aggregator = EpochAggregator()
        self.night = night
        self.batterySaver = batterySaver
        heartRatePaused = false
        UserDefaults.standard.set(night.id.uuidString, forKey: Self.activeNightKey)
        UserDefaults.standard.set(batterySaver, forKey: Self.batterySaverKey)
        scheduleAlarmSession(plan: plan, now: now)
        phase = .armed
        startTicking()
    }

    func disarm() { finish("Disarmed") }
    func stop() { finish("Stopped") }

    /// The app came to the foreground.
    func becameActive() {
        if phase != .idle { ensureSensors() }
        Task { await refreshStages() }
    }

    /// Apple's alarm session relaunched the app. It has to be claimed before the
    /// delegate call returns, or watchOS ends it.
    func adopt(_ session: WKExtendedRuntimeSession) {
        session.delegate = self
        alarmSession = session
        note("Relaunched by Apple's alarm session")
        guard phase != .idle else { return }
        ensureSensors()
        if session.state == .running { alarmSessionStarted(session) }
    }

    /// The app died mid-night while the workout kept going.
    func recoverWorkout() {
        ensureSensors()
    }

    private func finish(_ message: String) {
        tickTimer?.invalidate()
        tickTimer = nil
        stopLocalHaptics()
        // Only possible while the app is on screen; from the system alarm screen it's already over.
        if let alarmSession, alarmSession.state != .invalid { alarmSession.invalidate() }
        alarmSession = nil
        ringingViaSystem = false
        sensors.stop()
        append(aggregator.flush(upTo: .distantFuture))
        aggregator = EpochAggregator()
        night?.stoppedAt = Date()
        night?.batteryAtStop = batteryLevel
        note(message)
        UserDefaults.standard.removeObject(forKey: Self.activeNightKey)
        night = nil
        phase = .idle
        heartRate = nil
        history = store.all()
    }

    private func scheduleAlarmSession(plan: WakePlan, now: Date) {
        if let old = alarmSession, old.state != .invalid { old.invalidate() }
        let session = WKExtendedRuntimeSession()
        session.delegate = self
        let start = max(plan.alarmSessionStart, now.addingTimeInterval(5))
        session.start(at: start)
        alarmSession = session
        note("Apple's alarm session scheduled for \(start.shortTime)")
    }

    // MARK: The night

    private func startTicking() {
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    private func tick() {
        guard let plan = night?.plan else { return }
        let now = Date()
        append(aggregator.flush(upTo: now))
        if batterySaver { manageHeartRate(plan: plan, now: now) }
        switch phase {
        case .armed:
            if let epochs = night?.epochs, let reason = detector.evaluate(epochs, plan: plan, now: now) {
                ring(reason)
            }
        case .ringing:
            if let wake = night?.wake, now > wake.at.addingTimeInterval(Self.maxRinging) {
                finish("Rang for 30 minutes with no response")
            }
        case .idle:
            break
        }
    }

    private func manageHeartRate(plan: WakePlan, now: Date) {
        let wantPaused = now < plan.windowStart.addingTimeInterval(-Self.heartRateLead)
        guard wantPaused != heartRatePaused else { return }
        if wantPaused { sensors.pauseHeartRate() } else { sensors.resumeHeartRate() }
        heartRatePaused = wantPaused
    }

    private func append(_ epochs: [Epoch]) {
        guard !epochs.isEmpty, night != nil else { return }
        night?.epochs.append(contentsOf: epochs)
        moving = (epochs.last?.activity ?? 0) >= detector.config.movementFloor
        unsavedEpochs += epochs.count
        if unsavedEpochs >= 10 { save() }
    }

    /// After a relaunch the night is restored from disk but nothing is measuring: recover
    /// the workout if it survived, else start a new one, else settle for movement alone.
    private func ensureSensors() {
        guard night != nil, !sensors.isRunning, !startingSensors else { return }
        startingSensors = true
        Task {
            defer { startingSensors = false }
            if await sensors.recover() {
                note("Recovered the overnight workout")
                return
            }
            do {
                try sensors.start()
                note("Restarted the overnight workout")
            } catch {
                sensors.startMotion()
                note("Workout wouldn't restart (\(error.localizedDescription)); movement only")
            }
        }
    }

    private func restoreActiveNight() {
        guard let id = UserDefaults.standard.string(forKey: Self.activeNightKey).flatMap(UUID.init(uuidString:)),
              var saved = store.load(id: id), saved.stoppedAt == nil else { return }
        if Date() > saved.plan.deadline.addingTimeInterval(2 * 3600) {
            saved.stoppedAt = Date()
            saved.notes.append(Self.stamp("Closed on a later launch: the app wasn't running to stop it"))
            try? store.save(saved)
            UserDefaults.standard.removeObject(forKey: Self.activeNightKey)
            history = store.all()
            return
        }
        night = saved
        batterySaver = UserDefaults.standard.bool(forKey: Self.batterySaverKey)
        // Unknown after a relaunch; assuming paused makes the scheduled resume happen.
        heartRatePaused = batterySaver
        startTicking()
        ensureSensors()
        if let wake = saved.wake {
            phase = .ringing(wake.reason)
            ringLocally(from: HapticRamp.gentlePulses)
        } else {
            phase = .armed
        }
    }

    // MARK: Ringing

    private func ring(_ reason: WakeReason) {
        guard phase == .armed else { return }
        phase = .ringing(reason)
        night?.wake = WakeEvent(at: Date(), reason: reason)
        note("Ringing: \(reason.rawValue)")
        if let alarmSession, alarmSession.state == .running {
            ringViaSystem(alarmSession)
        } else {
            ringLocally(from: 0)
        }
    }

    /// Apple's alarm, which brings up the system alarm screen if the app isn't on screen.
    private func ringViaSystem(_ session: WKExtendedRuntimeSession) {
        stopLocalHaptics()
        ringingViaSystem = true
        let pulse = PulseCounter()
        session.notifyUser(hapticType: HapticRamp.step(0).strength.hapticType) { next in
            pulse.count += 1
            let step = HapticRamp.step(pulse.count)
            next.pointee = step.strength.hapticType
            return step.interval
        }
    }

    /// The app's own buzzing, for wakes before Apple's session starts or after it ends.
    private func ringLocally(from pulse: Int) {
        ringingViaSystem = false
        hapticPulse = pulse
        pulseLocally()
    }

    private func pulseLocally() {
        let step = HapticRamp.step(hapticPulse)
        WKInterfaceDevice.current().play(step.strength.hapticType)
        hapticPulse += 1
        hapticTimer = Timer.scheduledTimer(withTimeInterval: step.interval, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.pulseLocally() }
        }
    }

    private func stopLocalHaptics() {
        hapticTimer?.invalidate()
        hapticTimer = nil
    }

    // MARK: Apple's alarm session

    private func alarmSessionStarted(_ session: WKExtendedRuntimeSession) {
        guard session === alarmSession else { return }
        note("Apple's alarm session started")
        ensureSensors()
        if case .ringing = phase, !ringingViaSystem { ringViaSystem(session) }
    }

    private func alarmSessionExpiring(_ session: WKExtendedRuntimeSession) {
        // The deadline falls 5 minutes before expiry, so this only fires if a tick was missed.
        guard session === alarmSession, phase == .armed else { return }
        ring(.deadline)
    }

    private func alarmSessionEnded(_ session: WKExtendedRuntimeSession,
                                   reason: WKExtendedRuntimeSessionInvalidationReason, error: Error?) {
        guard session === alarmSession else { return }
        alarmSession = nil
        if case .ringing = phase, ringingViaSystem {
            if reason == .none {
                // The Stop button on the system alarm screen ends the session.
                finish("Stopped from the alarm screen")
            } else {
                // You slept through Apple's session: keep buzzing on the workout's runtime.
                note("Apple's alarm session ended (\(reason.label)); still buzzing")
                ringLocally(from: HapticRamp.gentlePulses)
            }
            return
        }
        let detail = error.map { ": \($0.localizedDescription)" } ?? ""
        note("Apple's alarm session ended (\(reason.label))\(detail)")
    }

    // MARK: Apple's sleep stages

    /// Fills in Apple's stages for finished nights once HealthKit has them. After a day
    /// with none, the night is marked as having none: on night one, that's the sign the
    /// overnight workout stopped Apple's own sleep tracking.
    func refreshStages() async {
        let now = Date()
        var changed = false
        for var log in store.all() {
            guard log.stages == nil, let stopped = log.stoppedAt, now > stopped.addingTimeInterval(10 * 60) else { continue }
            let from = log.plan.windowStart.addingTimeInterval(-3 * 3600)
            let to = max(log.plan.deadline, stopped).addingTimeInterval(3600)
            guard let intervals = try? await HealthStages.fetch(from: from, to: to, store: sensors.healthStore) else { continue }
            if StageReport.hasStages(intervals) {
                log.stages = intervals
            } else if now > stopped.addingTimeInterval(24 * 3600) {
                log.stages = intervals
                log.notes.append(Self.stamp("Apple recorded no sleep stages for this night"))
            } else {
                continue
            }
            try? store.save(log)
            changed = true
        }
        if changed { history = store.all() }
    }

    // MARK: Log

    private func note(_ text: String) {
        night?.notes.append(Self.stamp(text))
        save()
    }

    private func save() {
        guard let night else { return }
        try? store.save(night)
        unsavedEpochs = 0
    }

    private static func stamp(_ text: String) -> String {
        "\(Date().formatted(date: .omitted, time: .standard)) \(text)"
    }

    private var batteryLevel: Double? {
        let level = WKInterfaceDevice.current().batteryLevel
        return level >= 0 ? Double(level) : nil
    }
}

extension AlarmController: WKExtendedRuntimeSessionDelegate {
    nonisolated func extendedRuntimeSessionDidStart(_ extendedRuntimeSession: WKExtendedRuntimeSession) {
        Task { @MainActor in self.alarmSessionStarted(extendedRuntimeSession) }
    }

    nonisolated func extendedRuntimeSessionWillExpire(_ extendedRuntimeSession: WKExtendedRuntimeSession) {
        Task { @MainActor in self.alarmSessionExpiring(extendedRuntimeSession) }
    }

    nonisolated func extendedRuntimeSession(_ extendedRuntimeSession: WKExtendedRuntimeSession,
                                            didInvalidateWith reason: WKExtendedRuntimeSessionInvalidationReason,
                                            error: Error?) {
        Task { @MainActor in self.alarmSessionEnded(extendedRuntimeSession, reason: reason, error: error) }
    }
}

/// Counts pulses inside the system alarm's repeat handler.
private final class PulseCounter {
    var count = 0
}

extension HapticRamp.Strength {
    var hapticType: WKHapticType {
        self == .gentle ? .directionUp : .notification
    }
}

extension WKExtendedRuntimeSessionInvalidationReason {
    var label: String {
        switch self {
        case .none: return "ended normally"
        case .sessionInProgress: return "another session was already running"
        case .expired: return "hit its 30-minute limit"
        case .resignedFrontmost: return "app left the screen"
        case .suppressedBySystem: return "suppressed by watchOS"
        case .error: return "error"
        @unknown default: return "reason \(rawValue)"
        }
    }
}

extension Date {
    var shortTime: String { formatted(date: .omitted, time: .shortened) }
}
