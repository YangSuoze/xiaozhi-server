import random

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from plugins_func.functions.utils import handle_online_music_command

TAG = __name__

play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": "联网搜索并播放音乐、听歌。注意：只要用户要听歌、搜歌、播放歌、换首歌、放首歌、随机播首歌等都要必须调用这个工具！",
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称，歌手名。示例: ```用户:播放周杰伦的稻香\n参数：周杰伦 稻香```",
                },
                "userintent": {
                    "type": "string",
                    "description": "识别用户的意图。比如用户说换一首张学友的，用户的意图就是：播放一首和之前不重复的歌张学友的歌。意图要清晰明确",
                },
            },
            "required": ["song_name"],
        },
        "examples": [
            {
                "user_query": "播放一首歌",
                "answer": {
                    "song_name": "吻别（这里写你根据语境填写歌名或歌手或具体指令等搜索信息，不能为空字符串，要灵活一点比如用户说换一首别的歌就要适当改变搜索条件）",
                    "userintent": "用户想随机播放一首歌",
                },
            },
            {
                "user_query": "播放一首流行歌曲",
                "answer": {
                    "song_name": "流行歌曲",
                    "userintent": "用户想播放一首流行歌曲",
                },
            },
            {
                "user_query": "换一首",
                "answer": {
                    "song_name": "流行歌曲",
                    "userintent": "用户想播放一首流行歌曲，但用户想换一首，和之前播放过的XX、XX不重复",
                },
            },
            {
                "user_query": "放一首张学友的回头太难",
                "answer": {
                    "song_name": "张学友 回头太难",
                    "userintent": "用户想播放一首张学友的《回头太难》",
                },
            },
        ],
    },
}


@register_function("play_music", play_music_function_desc, ToolType.SYSTEM_CTL)
def play_music(conn, song_name: str, userintent: str = ""):
    try:
        if song_name == "random":
            recommendations = ["热歌", "抖音热歌", "经典老歌", "轻音乐"]
            song_name = random.choice(recommendations)

        if not conn.loop.is_running():
            return ActionResponse(
                action=Action.RESPONSE, result="系统繁忙", response="请稍后再试"
            )

        task = conn.loop.create_task(
            handle_online_music_command(conn, song_name, userintent)
        )

        def handle_done(f):
            try:
                f.result()
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放失败: {e}")

        task.add_done_callback(handle_done)

        return ActionResponse(
            action=Action.NONE,
            result="指令已接收",
            response=f"正在为您搜索 {song_name}",
        )
    except Exception as e:
        return ActionResponse(
            action=Action.RESPONSE, result=str(e), response="播放音乐时出错了"
        )
