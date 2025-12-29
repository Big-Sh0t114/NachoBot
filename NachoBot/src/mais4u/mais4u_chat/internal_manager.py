class InternalManager:
    def __init__(self):
        self.now_internal_state = str()

    def set_internal_state(self, internal_state: str):
        self.now_internal_state = internal_state

    def get_internal_state(self):
        return self.now_internal_state

    def get_internal_state_str(self):
        return f"你今天的直播内容是直播QQ水群，你正在一边回复弹幕，一边在QQ群聊天，你在QQ群聊天中产生的想法是：{self.now_internal_state}"


internal_manager = InternalManager()
