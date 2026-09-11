import threading


class ConnectionManager:
    _instance = None

    def __init__(self):
        # key: device_id, value: ConnectionHandler instance
        self.active_connections = {}
        self._lock = threading.RLock()

    @classmethod
    def get_instance(cls):
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def register(self, device_id, handler):
        """注册连接，并返回被替换的旧连接。"""
        if not device_id or handler is None:
            return None
        with self._lock:
            previous = self.active_connections.get(device_id)
            self.active_connections[device_id] = handler
            return previous

    def unregister(self, device_id, handler=None):
        """注销连接。

        传入 handler 时，仅删除仍指向该 handler 的登记，避免旧连接关闭时
        把同一设备刚建立的新连接一并删除。
        """
        if not device_id:
            return False
        with self._lock:
            current = self.active_connections.get(device_id)
            if current is None or (handler is not None and current is not handler):
                return False
            del self.active_connections[device_id]
            return True

    def get_handler(self, device_id):
        """获取指定设备的连接处理器"""
        with self._lock:
            return self.active_connections.get(device_id)

# 全局单例
connection_manager = ConnectionManager.get_instance()
