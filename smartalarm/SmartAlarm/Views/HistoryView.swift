import SmartAlarmCore
import SwiftUI

/// Past nights, each checked against Apple's sleep stages once HealthKit has them.
struct HistoryView: View {
    @EnvironmentObject private var alarm: AlarmController

    var body: some View {
        List {
            if alarm.history.isEmpty {
                Text("No nights yet.").foregroundStyle(.secondary)
            }
            ForEach(alarm.history) { night in
                NavigationLink {
                    NightDetailView(night: night)
                } label: {
                    NightRow(night: night)
                }
            }
        }
        .navigationTitle("Past nights")
        .task { await alarm.refreshStages() }
    }
}

struct NightRow: View {
    let night: NightLog

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(night.plan.deadline.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day()))
                .font(.headline)
            if let wake = night.wake {
                Text("Up \(wake.at.shortTime) · \(night.minutesEarly ?? 0) min early").font(.footnote)
            } else {
                Text("No alarm").font(.footnote)
            }
            Text(appleSays).font(.footnote).foregroundStyle(.secondary)
        }
    }

    private var appleSays: String {
        guard night.stages != nil else { return "Apple's stages not in yet" }
        guard night.wake != nil else { return "" }
        return night.stageAtWake.map { "Apple: \($0.label) at wake" } ?? "Apple: no stage at wake"
    }
}

struct NightDetailView: View {
    let night: NightLog

    var body: some View {
        List {
            if let wake = night.wake {
                Section("Woke you") {
                    Text("\(wake.at.shortTime) · \(wake.reason.explanation)")
                    if let stage = night.stageAtWake {
                        Text("Apple says: \(stage.label)")
                    }
                }
            }
            if let minutes = night.windowStageMinutes, !minutes.isEmpty {
                Section("Window, per Apple") {
                    ForEach(SleepStage.allCases.filter { minutes[$0] != nil }, id: \.self) { stage in
                        HStack {
                            Text(stage.label)
                            Spacer()
                            Text("\(Int(minutes[stage]!.rounded())) min")
                        }
                    }
                }
            }
            if let start = night.batteryAtArm, let end = night.batteryAtStop {
                Section("Battery") {
                    Text("\(Int(start * 100))% → \(Int(end * 100))%")
                }
            }
            Section("Log") {
                ForEach(Array(night.notes.enumerated()), id: \.offset) { _, note in
                    Text(note).font(.footnote)
                }
            }
            ShareLink(item: night.exportText) {
                Label("Share data", systemImage: "square.and.arrow.up")
            }
        }
        .navigationTitle(night.plan.deadline.formatted(.dateTime.month(.abbreviated).day()))
    }
}
