import Foundation

/// How hard to buzz on the n-th pulse of an alarm: a few gentle taps first (you may
/// already be near the surface), then firm and frequent until you stop it.
public enum HapticRamp {
    public enum Strength: Equatable, Sendable {
        case gentle, firm
    }

    public static let gentlePulses = 6

    public static func step(_ pulse: Int) -> (strength: Strength, interval: TimeInterval) {
        pulse < gentlePulses ? (.gentle, 4) : (.firm, 2)
    }
}
