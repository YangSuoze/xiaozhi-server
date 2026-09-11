"""向已连接设备投递主动文本播报。"""

import uuid

from core.handle.sendAudioHandle import send_stt_message
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message


class DeviceNotReadyError(RuntimeError):
    """设备连接尚未具备主动播报条件。"""


async def queue_text_notification(conn, text: str) -> str:
    """把一条文本提醒加入设备的显示和 TTS 队列。

    返回此次投递 ID。这里的成功代表服务端已完成入队，不代表设备已经播放；
    将来固件增加确认消息后，可用同一个 ID 完成端到端确认。
    """
    if conn is None or getattr(conn, "websocket", None) is None:
        raise DeviceNotReadyError("设备未连接")

    tts = getattr(conn, "tts", None)
    text_queue = getattr(tts, "tts_text_queue", None)
    if text_queue is None:
        raise DeviceNotReadyError("设备语音组件尚未就绪")

    delivery_id = uuid.uuid4().hex
    await send_stt_message(conn, text)

    dialogue = getattr(conn, "dialogue", None)
    if dialogue is not None:
        dialogue.put(Message(role="assistant", content=text))

    text_queue.put(
        TTSMessageDTO(
            sentence_id=delivery_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    text_queue.put(
        TTSMessageDTO(
            sentence_id=delivery_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )
    text_queue.put(
        TTSMessageDTO(
            sentence_id=delivery_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    return delivery_id
