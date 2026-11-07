# Smart Alarm (Apple Watch)

A watch-only app that wakes you at a good moment inside a window you choose (30–120
minutes, e.g. 6:00–7:00) instead of at a fixed minute, and checks each wake against
Apple's own sleep stages the next morning.

## How it works

**Apple's REM data can't drive it.** The watch writes its REM/Core/Deep stages to
HealthKit in one batch after the night ends ([Apple DTS, Oct 2026](https://developer.apple.com/forums/thread/849362)),
so no app can read "you're in REM now." The app reads the two signals it *can* get live,
wrist movement and heart rate, and looks for the moment you leave deep sleep:

| Trigger | Signal | Usually means |
|---|---|---|
| `awake` | movement above 0.06 g for 1.5 min | you're up |
| `movement` | 2 of the last 3 half-minutes moving ≥ 4× your still baseline (and ≥ 0.015 g) | arousal into light sleep |
| `heartRate` | 2-min average ≥ 6 bpm above your sleeping baseline (25th percentile of the hour before the window) | REM or light sleep |
| `deadline` | none of the above by your latest time | — |

Deep sleep (still, low and steady heart rate) never fires a trigger. The thresholds
in `DetectorConfig` are starting guesses. They get tuned once there are real nights to
compare against Apple's stages.

**Two layers cover a window longer than Apple's 30-minute cap:**

1. **An overnight workout session**, started when you arm it. This is the only way a
   watch app stays running for hours. It streams heart rate and keeps the accelerometer
   on. If the detector fires early in the window, the app buzzes with its own haptics.
   The workout is discarded at the end, so nothing appears in Fitness.
2. **Apple's smart-alarm session** (`WKExtendedRuntimeSession`), scheduled for the last
   25 minutes before your deadline plus 5 minutes after it. Ringing here uses the real
   system alarm screen. Because watchOS relaunches the app for this session, the
   deadline alarm still fires even if the app died in the night. If an early buzz didn't
   wake you, the alarm escalates to this screen once the session starts.

The haptics start with a few gentle taps, then turn firm until you tap Stop. After 30
minutes with no response the alarm gives up, so keep a normal alarm as a backup during
the first week.

## Before you can install it

- **Current macOS + Xcode.** Xcode 15 / macOS 14.0 can't install apps on a watch
  running current watchOS. Update macOS, then install Xcode from the App Store.
- **Apple Developer Program ($99/yr).** Apps signed with a free account stop launching
  after 7 days. For an alarm, that's a silent failure.
- **Developer Mode** on the iPhone and the watch (Settings → Privacy & Security →
  Developer Mode), which appears after the first install attempt from Xcode.

## Install

1. Open `SmartAlarm.xcodeproj`.
2. Target **SmartAlarm** → Signing & Capabilities → pick your team. If Xcode says the
   bundle ID is taken, change `com.ssethia2.aria.smartalarm` to anything unique.
3. Pick your watch as the run destination (the iPhone must be connected or on the same
   network) and press Run.
4. On the first Arm, allow Health access (heart rate, sleep, workouts).

## Use

- **Every night:** open the app, check the time and window, tap **Arm**. watchOS only
  lets an app schedule its alarm session while it's on screen, so this can't be
  automated from the watch alone.
- **Try it (3 min):** arms a test that rings within 3 minutes, sooner if you move your
  wrist. Run it once before trusting a real night.
- **Battery saver (experimental):** pauses the workout, which turns the heart-rate
  sensor off, until 30 minutes before the window. Movement keeps recording if watchOS
  keeps a paused workout's app running. The log shows whether it did.
- **Past nights:** when you woke, why, and what Apple says you were in at that moment,
  once HealthKit has the stages (some time after you get up). **Share data** sends a
  night as plain text for tuning.

## Night-one checklist

These depend on watchOS behavior that can't be tested without a watch. Each one shows
up in a night's **Log**:

- [ ] **"Apple's alarm session started"** at deadline − 25 min. If it says *another
      session was already running* instead, watchOS won't run the alarm session
      alongside a workout. The app still rings at the deadline with its own haptics,
      but without the system alarm screen.
- [ ] **Early buzzes wake you** with the screen off (haptics from a background workout).
- [ ] **Battery**: the Battery line shows the overnight drain.
- [ ] **Apple's stages show up** for the night. *"Apple recorded no sleep stages"* means
      the workout stopped Apple's own sleep tracking. The fix would be to start the
      workout at the window instead of at bedtime, triggered from the iPhone.

## Layout

```
SmartAlarmCore/   Detector, epochs, plan, stages, night log: plain Swift, no WatchKit.
                  `swift test` runs on a Mac.
SmartAlarm/       The watch app: sensors (workout + accelerometer), Apple's alarm
                  session, haptics, screens.
project.yml       XcodeGen spec. The generated .xcodeproj is committed; regenerate
                  with `xcodegen generate` after editing this file.
```
