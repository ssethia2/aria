import SmartAlarmCore
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var alarm: AlarmController
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationStack {
            switch alarm.phase {
            case .idle:
                ArmView()
            case .armed:
                MonitoringView()
            case .ringing(let reason):
                RingingView(reason: reason)
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { alarm.becameActive() }
        }
    }
}

struct ArmView: View {
    @EnvironmentObject private var alarm: AlarmController
    @AppStorage("wakeHour") private var hour = 7
    @AppStorage("wakeMinute") private var minute = 0
    @AppStorage("windowMinutes") private var windowMinutes = 60
    @AppStorage("batterySaver") private var batterySaver = false
    @State private var arming = false

    private var plan: WakePlan {
        WakePlan.next(hour: hour, minute: minute, windowMinutes: windowMinutes, after: Date())
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                HStack(spacing: 2) {
                    Picker("Hour", selection: $hour) {
                        ForEach(0..<24, id: \.self) { Text(String(format: "%02d", $0)).tag($0) }
                    }
                    Text(":").font(.title3)
                    Picker("Minute", selection: $minute) {
                        ForEach(Array(stride(from: 0, to: 60, by: 5)), id: \.self) { Text(String(format: "%02d", $0)).tag($0) }
                    }
                }
                .pickerStyle(.wheel)
                .labelsHidden()
                .frame(height: 70)

                Text("Wakes you between \(plan.windowStart.shortTime) and \(plan.deadline.shortTime)")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                Stepper(value: $windowMinutes, in: 30...120, step: 15) {
                    Text("\(windowMinutes) min").font(.headline)
                }

                Button {
                    arming = true
                    Task {
                        await alarm.arm(hour: hour, minute: minute, windowMinutes: windowMinutes, batterySaver: batterySaver)
                        arming = false
                    }
                } label: {
                    Text(arming ? "Arming…" : "Arm").font(.headline).frame(maxWidth: .infinity)
                }
                .tint(.indigo)
                .disabled(arming)

                if let error = alarm.error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }

                Button("Try it (3 min)") {
                    arming = true
                    Task {
                        await alarm.armTest()
                        arming = false
                    }
                }
                .font(.footnote)
                .disabled(arming)

                Toggle(isOn: $batterySaver) {
                    VStack(alignment: .leading) {
                        Text("Battery saver")
                        Text("Heart rate off until 30 min before the window. Experimental.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }

                NavigationLink("Past nights") { HistoryView() }
            }
        }
        .navigationTitle("Smart Alarm")
    }
}

struct MonitoringView: View {
    @EnvironmentObject private var alarm: AlarmController

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                if let plan = alarm.night?.plan {
                    Label("From \(plan.windowStart.shortTime)", systemImage: "moon.zzz.fill")
                    Label("Up by \(plan.deadline.shortTime)", systemImage: "alarm.fill")
                }
                HStack {
                    Label(alarm.heartRate.map { "\(Int($0.rounded()))" } ?? "–", systemImage: "heart.fill")
                        .foregroundStyle(.pink)
                    Spacer()
                    Text(alarm.moving ? "Moving" : "Still").foregroundStyle(.secondary)
                }
                Text("\(alarm.night?.epochs.count ?? 0) readings")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                if let last = alarm.night?.notes.last {
                    Text(last).font(.footnote).foregroundStyle(.secondary)
                }
                Button("Disarm", role: .destructive) { alarm.disarm() }
                    .padding(.top, 4)
            }
        }
        .navigationTitle("Armed")
    }
}

struct RingingView: View {
    @EnvironmentObject private var alarm: AlarmController
    let reason: WakeReason

    var body: some View {
        VStack(spacing: 10) {
            Text("Good morning").font(.title3.bold())
            Text(reason.explanation)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button {
                alarm.stop()
            } label: {
                Text("Stop").font(.title2.bold()).frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(.orange)
        }
    }
}

extension WakeReason {
    var explanation: String {
        switch self {
        case .awake: return "You looked awake already."
        case .movement: return "You were stirring: a light moment to get up."
        case .heartRate: return "Your heart rate picked up: likely REM or light sleep."
        case .deadline: return "Your latest wake time."
        }
    }
}
