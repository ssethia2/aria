import SwiftUI
import WatchKit

@main
struct SmartAlarmApp: App {
    @WKApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(AlarmController.shared)
        }
    }
}

final class AppDelegate: NSObject, WKApplicationDelegate {
    func handle(_ extendedRuntimeSession: WKExtendedRuntimeSession) {
        AlarmController.shared.adopt(extendedRuntimeSession)
    }

    func handleActiveWorkoutRecovery() {
        AlarmController.shared.recoverWorkout()
    }
}
