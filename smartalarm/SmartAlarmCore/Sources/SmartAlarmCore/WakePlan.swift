import Foundation

/// One night's alarm: the latest acceptable wake time and how long before it the app may wake you.
public struct WakePlan: Codable, Equatable, Sendable {
    /// Apple caps a smart-alarm session at 30 minutes.
    public static let alarmSessionLength: TimeInterval = 30 * 60
    /// The session is started this long before it would have to, so it is still alive
    /// to keep ringing past the deadline instead of expiring the moment it rings.
    public static let ringGrace: TimeInterval = 5 * 60

    public var deadline: Date
    public var windowMinutes: Int

    public init(deadline: Date, windowMinutes: Int) {
        self.deadline = deadline
        self.windowMinutes = windowMinutes
    }

    public var windowStart: Date {
        deadline.addingTimeInterval(-Double(windowMinutes) * 60)
    }

    /// When Apple's smart-alarm session should begin: the tail of the window, ending
    /// `ringGrace` after the deadline. The workout session covers everything before it.
    public var alarmSessionStart: Date {
        max(windowStart, deadline.addingTimeInterval(-(Self.alarmSessionLength - Self.ringGrace)))
    }

    /// The next `hour:minute` after `now`: today if it is still ahead, otherwise tomorrow.
    public static func next(hour: Int, minute: Int, windowMinutes: Int,
                            after now: Date, calendar: Calendar = .current) -> WakePlan {
        let components = DateComponents(hour: hour, minute: minute, second: 0)
        let deadline = calendar.nextDate(after: now, matching: components, matchingPolicy: .nextTime)
            ?? now.addingTimeInterval(24 * 3600)
        return WakePlan(deadline: deadline, windowMinutes: windowMinutes)
    }
}
