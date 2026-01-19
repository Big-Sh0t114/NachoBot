"""
聊天总结插件常量配置

集中管理所有硬编码的配置项，提高可维护性
"""

from typing import List, Tuple


class FontConfig:
    """字体配置"""

    # 字体路径列表（按优先级排序）
    FONT_PATHS: List[str] = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",  # macOS
        "C:/Windows/Fonts/msyh.ttc",  # Windows
    ]


class ColorScheme:
    """配色方案 - 梦幻渐变风格"""

    # 背景渐变色
    BG_START: Tuple[int, int, int] = (178, 255, 255)  # 清亮蓝
    BG_MID: Tuple[int, int, int] = (216, 222, 236)    # 淡粉蓝
    BG_END: Tuple[int, int, int] = (255, 188, 217)    # 粉色

    # 卡片配色
    CARD_BG: Tuple[int, int, int, int] = (255, 255, 255, 250)      # 白色半透明
    CARD_BG_LIGHT: Tuple[int, int, int, int] = (250, 250, 255, 245)  # 稍紫的卡片

    # 彩色边框
    BORDER_CYAN: Tuple[int, int, int] = (95, 210, 220)
    BORDER_MAGENTA: Tuple[int, int, int] = (255, 135, 195)
    BORDER_YELLOW: Tuple[int, int, int] = (255, 205, 150)
    BORDER_GREEN: Tuple[int, int, int] = (140, 230, 200)
    BORDER_PINK: Tuple[int, int, int] = (255, 165, 205)
    BORDER_ORANGE: Tuple[int, int, int] = (255, 185, 145)
    BORDER_PURPLE: Tuple[int, int, int] = (205, 155, 235)
    BORDER_BLUE: Tuple[int, int, int] = (120, 205, 230)

    # 文字颜色
    TITLE_COLOR: Tuple[int, int, int] = (80, 60, 120)
    TEXT_COLOR: Tuple[int, int, int] = (60, 60, 80)
    SUBTITLE_COLOR: Tuple[int, int, int] = (100, 100, 130)
    LIGHT_TEXT_COLOR: Tuple[int, int, int] = (130, 130, 150)
    HIGHLIGHT_COLOR: Tuple[int, int, int] = (255, 135, 195)

    # 渐变强调色
    GRADIENT_1_START: Tuple[int, int, int] = (135, 235, 245)
    GRADIENT_1_END: Tuple[int, int, int] = (95, 210, 220)

    GRADIENT_2_START: Tuple[int, int, int] = (255, 170, 215)
    GRADIENT_2_END: Tuple[int, int, int] = (255, 135, 195)

    GRADIENT_3_START: Tuple[int, int, int] = (255, 210, 175)
    GRADIENT_3_END: Tuple[int, int, int] = (255, 185, 145)


class LayoutConfig:
    """布局配置"""

    # 尺寸
    WIDTH: int = 1200
    PADDING: int = 70
    CARD_PADDING: int = 45
    CARD_SPACING: int = 35

    # 字体大小
    TITLE_SIZE: int = 64
    SECTION_TITLE_SIZE: int = 46
    SUBTITLE_SIZE: int = 32
    TEXT_SIZE: int = 28
    SMALL_SIZE: int = 24


class DecorationConfig:
    """装饰配置"""

    # 相对于插件目录的装饰图片路径
    DECORATION_DIR: str = "decorations"

    # 装饰图片文件名
    DECORATION_1: str = "decoration1.png"
    DECORATION_2: str = "decoration2.png"
    DECORATION_3: str = "decoration3.png"
    DECORATION_4: str = "decoration4.png"
    DECORATION_5: str = "decoration5.png"
    DECORATION_STAR: str = "decoration_star.png"
    DECORATION_SPARKLE: str = "decoration_sparkle.png"
    DECORATION_HEART: str = "decoration_heart.png"
    DECORATION_BUBBLE: str = "decoration_bubble.png"
    DECORATION_QUOTE: str = "decoration_quote.png"


class AnalysisConfig:
    """分析配置"""

    # 用户称号分析
    MIN_MESSAGES_FOR_TITLE: int = 5  # 最少发言数才分析称号
    MAX_USERS_FOR_TITLE: int = 8     # 最多分析用户数

    # 金句提取
    MIN_QUOTE_LENGTH: int = 5        # 金句最小长度
    MAX_QUOTE_LENGTH: int = 100      # 金句最大长度
    MIN_QUOTES: int = 3              # 最少金句数
    MAX_QUOTES: int = 5              # 最多金句数

    # JSON 返回验证
    MAX_REASON_LENGTH: int = 100     # 理由最大长度（防止LLM返回过长，控制在70字左右）
    MAX_TITLE_LENGTH: int = 10       # 称号最大长度
