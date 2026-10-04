from core.utils.tts_timing import get_audio_end_grace_seconds


def test_audio_end_grace_uses_configured_value():
    assert get_audio_end_grace_seconds(
        {"tts_audio_end_grace_ms": 1200}, frame_duration_ms=60, pre_buffer_count=5
    ) == 1.2


def test_audio_end_grace_keeps_safe_minimum():
    assert get_audio_end_grace_seconds(
        {"tts_audio_end_grace_ms": 100}, frame_duration_ms=60, pre_buffer_count=5
    ) == 0.42


def test_audio_end_grace_handles_invalid_config():
    assert get_audio_end_grace_seconds(
        {"tts_audio_end_grace_ms": "invalid"},
        frame_duration_ms=60,
        pre_buffer_count=5,
    ) == 0.42
