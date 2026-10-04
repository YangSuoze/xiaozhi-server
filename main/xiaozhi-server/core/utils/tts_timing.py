"""Timing helpers shared by TTS transport code."""


def get_audio_end_grace_seconds(
    config: dict, frame_duration_ms: int, pre_buffer_count: int
) -> float:
    minimum_grace_ms = (pre_buffer_count + 2) * frame_duration_ms
    configured_grace_ms = config.get("tts_audio_end_grace_ms", minimum_grace_ms)
    try:
        configured_grace_ms = int(configured_grace_ms)
    except (TypeError, ValueError):
        configured_grace_ms = minimum_grace_ms
    return max(minimum_grace_ms, configured_grace_ms) / 1000.0
