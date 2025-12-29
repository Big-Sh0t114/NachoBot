class ScreenManager:
    def __init__(self):
        self.now_screen = str()

    def set_screen(self, screen_str: str):
        self.now_screen = screen_str

    def get_screen(self):
        return self.now_screen

    def get_screen_str(self):
        return f"你可以看见面前的屏幕，目前屏幕的内容是:现在千石可乐在和你一起直播，这是他正在操作的屏幕内容：{self.now_screen}"


screen_manager = ScreenManager()
