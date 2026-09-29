from wav2sum.autorecord import CallDetector

ZOOM = "us.zoom.xos"


def _detector():
    detector = CallDetector(["us.zoom", "com.microsoft.teams2"], start_after=3, stop_after=10)
    detector.observe([], now=0)
    return detector


def test_starts_once_call_app_holds_the_mic_and_stops_after_it_lets_go():
    detector = _detector()
    detector.observe(["com.apple.Safari", ZOOM], now=1)
    assert detector.app == ZOOM
    assert detector.decide(None, now=3) is None
    assert detector.decide(None, now=4) == "start"

    detector.observe([], now=60)
    assert detector.decide("auto", now=65) is None
    assert detector.decide("auto", now=70) == "stop"


def test_brief_mic_release_does_not_stop_recording():
    detector = _detector()
    detector.observe([ZOOM], now=1)
    detector.observe([], now=30)
    detector.observe([ZOOM], now=35)
    assert detector.decide("auto", now=45) is None


def test_ignores_other_apps_and_manual_recordings():
    detector = _detector()
    detector.observe(["ru.keepcoder.Telegram", "us.zoomer.app"], now=1)
    assert detector.decide(None, now=20) is None

    detector.observe([ZOOM], now=21)
    assert detector.decide("manual", now=30) is None
    detector.observe([], now=31)
    assert detector.decide("manual", now=60) is None


def test_dismissed_call_is_not_recorded_again_until_it_ends():
    detector = _detector()
    detector.observe([ZOOM], now=1)
    assert detector.decide(None, now=5) == "start"
    detector.dismiss()
    assert detector.decide(None, now=30) is None

    detector.observe([], now=40)
    assert detector.decide(None, now=50) is None
    detector.observe([ZOOM], now=100)
    assert detector.decide(None, now=104) == "start"
