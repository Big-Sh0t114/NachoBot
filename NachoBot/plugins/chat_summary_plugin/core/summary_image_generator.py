"""
聊天总结图片生成器 - 梦幻渐变风格
明亮温暖的视觉设计，充分利用所有装饰元素
"""

import os
import io
import base64
import tempfile
import aiohttp
import asyncio
from typing import Tuple, List, Optional
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .constants import FontConfig, ColorScheme, LayoutConfig, DecorationConfig

# 导入logger
try:
    from src.common.logger import get_logger
    logger = get_logger("summary_image_generator")
except ImportError:
    import logging
    logger = logging.getLogger("summary_image_generator")


class SummaryImageGenerator:
    """生成聊天总结图片 - 梦幻渐变风格"""

    # 从常量配置导入（保持向后兼容）
    BG_START = ColorScheme.BG_START
    BG_MID = ColorScheme.BG_MID
    BG_END = ColorScheme.BG_END
    CARD_BG = ColorScheme.CARD_BG
    CARD_BG_LIGHT = ColorScheme.CARD_BG_LIGHT
    BORDER_CYAN = ColorScheme.BORDER_CYAN
    BORDER_MAGENTA = ColorScheme.BORDER_MAGENTA
    BORDER_YELLOW = ColorScheme.BORDER_YELLOW
    BORDER_GREEN = ColorScheme.BORDER_GREEN
    BORDER_PINK = ColorScheme.BORDER_PINK
    BORDER_ORANGE = ColorScheme.BORDER_ORANGE
    BORDER_PURPLE = ColorScheme.BORDER_PURPLE
    BORDER_BLUE = ColorScheme.BORDER_BLUE
    TITLE_COLOR = ColorScheme.TITLE_COLOR
    TEXT_COLOR = ColorScheme.TEXT_COLOR
    SUBTITLE_COLOR = ColorScheme.SUBTITLE_COLOR
    LIGHT_TEXT_COLOR = ColorScheme.LIGHT_TEXT_COLOR
    HIGHLIGHT_COLOR = ColorScheme.HIGHLIGHT_COLOR
    GRADIENT_1_START = ColorScheme.GRADIENT_1_START
    GRADIENT_1_END = ColorScheme.GRADIENT_1_END
    GRADIENT_2_START = ColorScheme.GRADIENT_2_START
    GRADIENT_2_END = ColorScheme.GRADIENT_2_END
    GRADIENT_3_START = ColorScheme.GRADIENT_3_START
    GRADIENT_3_END = ColorScheme.GRADIENT_3_END

    WIDTH = LayoutConfig.WIDTH
    PADDING = LayoutConfig.PADDING
    CARD_PADDING = LayoutConfig.CARD_PADDING
    CARD_SPACING = LayoutConfig.CARD_SPACING
    TITLE_SIZE = LayoutConfig.TITLE_SIZE
    SECTION_TITLE_SIZE = LayoutConfig.SECTION_TITLE_SIZE
    SUBTITLE_SIZE = LayoutConfig.SUBTITLE_SIZE
    TEXT_SIZE = LayoutConfig.TEXT_SIZE
    SMALL_SIZE = LayoutConfig.SMALL_SIZE

    @staticmethod
    def _get_font(size: int) -> ImageFont.FreeTypeFont:
        """获取字体"""
        for path in FontConfig.FONT_PATHS:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue

        raise RuntimeError("未找到可用的中文字体")

    @staticmethod
    async def _download_qq_avatar(qq_id: str, size: int = 100) -> Optional[Image.Image]:
        """下载QQ用户头像

        Args:
            qq_id: QQ号
            size: 头像尺寸，可选 40, 100, 140, 640

        Returns:
            PIL Image对象，失败返回 None
        """
        if not qq_id:
            return None

        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq_id}&s={size}"
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(avatar_url) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        return Image.open(io.BytesIO(avatar_data)).convert("RGBA")
        except Exception as e:
            logger.debug(f"下载头像失败 (QQ:{qq_id}): {e}")

        return None

    @staticmethod
    def _create_circular_avatar(avatar: Image.Image, size: int) -> Image.Image:
        """将头像裁剪为圆形

        Args:
            avatar: 原始头像图片
            size: 目标尺寸

        Returns:
            圆形头像
        """
        # 调整大小
        avatar = avatar.resize((size, size), Image.Resampling.LANCZOS)

        # 创建圆形蒙版
        mask = Image.new('L', (size, size), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, size, size), fill=255)

        # 应用蒙版
        output = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        output.paste(avatar, (0, 0))
        output.putalpha(mask)

        return output

    @staticmethod
    def _add_avatar_glow(
        img: Image.Image,
        avatar: Image.Image,
        position: Tuple[int, int],
        glow_color: Tuple[int, int, int],
        glow_radius: int = 8
    ) -> Image.Image:
        """添加带光晕效果的圆形头像

        Args:
            img: 目标图片
            avatar: 圆形头像
            position: 粘贴位置 (x, y)
            glow_color: 光晕颜色 RGB
            glow_radius: 光晕半径

        Returns:
            添加头像后的图片
        """
        size = avatar.size[0]

        # 创建光晕层
        glow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        for offset in range(glow_radius, 0, -1):
            alpha = int(50 * (glow_radius - offset) / glow_radius)
            glow_size = size + offset * 2
            glow_temp = Image.new('RGBA', (glow_size, glow_size), (0, 0, 0, 0))

            # 创建光晕圆形
            glow_mask = Image.new('L', (glow_size, glow_size), 0)
            glow_mask_draw = ImageDraw.Draw(glow_mask)
            glow_mask_draw.ellipse((0, 0, glow_size, glow_size), fill=255)

            # 应用颜色
            glow_colored = Image.new('RGBA', (glow_size, glow_size), glow_color + (alpha,))
            glow_temp.paste(glow_colored, (0, 0), glow_mask)

            # 粘贴到光晕层
            glow_x = position[0] - offset
            glow_y = position[1] - offset
            glow_layer.paste(glow_temp, (glow_x, glow_y), glow_temp)

        # 模糊光晕
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=glow_radius // 2))

        # 合成光晕
        img = Image.alpha_composite(img, glow_layer)

        # 添加白色边框
        border_size = size + 6
        border_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        border_draw = ImageDraw.Draw(border_layer)
        border_x = position[0] - 3
        border_y = position[1] - 3
        border_draw.ellipse(
            (border_x, border_y, border_x + border_size, border_y + border_size),
            fill=(255, 255, 255, 200)
        )
        img = Image.alpha_composite(img, border_layer)

        # 粘贴头像
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        overlay.paste(avatar, position, avatar)
        img = Image.alpha_composite(img, overlay)

        return img

    @staticmethod
    def _draw_rounded_rectangle(
        draw: ImageDraw.ImageDraw,
        coords: tuple,
        radius: int,
        fill: tuple,
        outline: tuple = None,
        width: int = 1
    ):
        """绘制圆角矩形"""
        x1, y1, x2, y2 = coords

        # 绘制主体矩形
        draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
        draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)

        # 四个圆角
        draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
        draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
        draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
        draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)

        # 绘制边框
        if outline:
            draw.arc([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=outline, width=width)
            draw.arc([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=outline, width=width)
            draw.line([x1 + radius, y1, x2 - radius, y1], fill=outline, width=width)
            draw.line([x1 + radius, y2, x2 - radius, y2], fill=outline, width=width)
            draw.line([x1, y1 + radius, x1, y2 - radius], fill=outline, width=width)
            draw.line([x2, y1 + radius, x2, y2 - radius], fill=outline, width=width)

    @staticmethod
    def _wrap_text(text: str, max_width: int, font: ImageFont.FreeTypeFont) -> List[str]:
        """文本自动换行"""
        lines = []

        for paragraph in text.split('\n'):
            if not paragraph.strip():
                lines.append('')
                continue

            current_line = ''
            for char in paragraph:
                test_line = current_line + char
                bbox = font.getbbox(test_line)
                w = bbox[2] - bbox[0]

                if w <= max_width:
                    current_line = test_line
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = char

            if current_line:
                lines.append(current_line)

        return lines

    @staticmethod
    def _draw_gradient_rect(
        draw: ImageDraw.ImageDraw,
        coords: tuple,
        start_color: tuple,
        end_color: tuple,
        horizontal: bool = True
    ):
        """绘制渐变矩形"""
        x1, y1, x2, y2 = coords

        if horizontal:
            # 水平渐变
            for x in range(x1, x2):
                ratio = (x - x1) / max(1, (x2 - x1))
                r = int(start_color[0] + (end_color[0] - start_color[0]) * ratio)
                g = int(start_color[1] + (end_color[1] - start_color[1]) * ratio)
                b = int(start_color[2] + (end_color[2] - start_color[2]) * ratio)
                draw.line([(x, y1), (x, y2)], fill=(r, g, b))
        else:
            # 垂直渐变
            for y in range(y1, y2):
                ratio = (y - y1) / max(1, (y2 - y1))
                r = int(start_color[0] + (end_color[0] - start_color[0]) * ratio)
                g = int(start_color[1] + (end_color[1] - start_color[1]) * ratio)
                b = int(start_color[2] + (end_color[2] - start_color[2]) * ratio)
                draw.line([(x1, y), (x2, y)], fill=(r, g, b))

    @staticmethod
    def _draw_colorful_card(
        img: Image.Image,
        coords: tuple,
        border_color: tuple,
        radius: int = 20,
        shadow_strength: int = 15,
        use_gradient_bg: bool = True,
        use_rainbow_border: bool = True
    ) -> Image.Image:
        """绘制彩色卡片（适合明亮背景）- 升级版：渐变背景 + 彩虹边框

        Args:
            img: 目标图片
            coords: 卡片坐标 (x1, y1, x2, y2)
            border_color: 边框颜色（用于确定主色调）
            radius: 圆角半径
            shadow_strength: 阴影强度
            use_gradient_bg: 是否使用渐变背景
            use_rainbow_border: 是否使用彩虹渐变边框
        """
        x1, y1, x2, y2 = coords

        # 创建RGBA图层
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)

        # 绘制柔和阴影效果（从外到内）
        for i in range(shadow_strength, 0, -1):
            alpha = int(30 * (shadow_strength - i) / shadow_strength)
            shadow_color = (100, 100, 120, alpha)
            offset = i

            SummaryImageGenerator._draw_rounded_rectangle(
                overlay_draw,
                (x1 - offset + 2, y1 - offset + 2, x2 + offset + 2, y2 + offset + 2),
                radius + offset,
                fill=(0, 0, 0, 0),
                outline=shadow_color,
                width=2
            )

        # 应用模糊
        overlay = overlay.filter(ImageFilter.GaussianBlur(radius=shadow_strength // 2))

        # 合并到主图
        img = img.convert('RGBA')
        img = Image.alpha_composite(img, overlay)

        # 绘制卡片背景 - 微妙渐变效果
        if use_gradient_bg:
            # 创建渐变背景（从顶部到底部：淡蓝紫 -> 纯白 -> 淡粉）
            bg_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            card_height = y2 - y1

            for i in range(card_height):
                ratio = i / card_height
                # 三段渐变
                if ratio < 0.3:
                    # 顶部：淡蓝紫
                    progress = ratio / 0.3
                    r = int(252 + (255 - 252) * progress)
                    g = int(250 + (255 - 250) * progress)
                    b = int(255)
                    alpha = 250
                elif ratio < 0.7:
                    # 中部：纯白
                    r, g, b = 255, 255, 255
                    alpha = 250
                else:
                    # 底部：淡粉
                    progress = (ratio - 0.7) / 0.3
                    r = int(255)
                    g = int(255 - 3 * progress)
                    b = int(255 - 2 * progress)
                    alpha = 250

                # 只在卡片区域内绘制
                overlay_line = Image.new('RGBA', img.size, (0, 0, 0, 0))
                overlay_line_draw = ImageDraw.Draw(overlay_line)
                overlay_line_draw.line([(x1, y1 + i), (x2, y1 + i)], fill=(r, g, b, alpha))
                bg_layer = Image.alpha_composite(bg_layer, overlay_line)

            # 应用圆角蒙版
            mask = Image.new('L', img.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            SummaryImageGenerator._draw_rounded_rectangle(
                mask_draw,
                coords,
                radius,
                fill=255
            )
            bg_layer.putalpha(mask)
            img = Image.alpha_composite(img, bg_layer)
        else:
            # 使用纯色背景
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            SummaryImageGenerator._draw_rounded_rectangle(
                overlay_draw,
                coords,
                radius,
                fill=SummaryImageGenerator.CARD_BG
            )
            img = Image.alpha_composite(img, overlay)

        # 绘制边框 - 彩虹渐变或单色
        if use_rainbow_border:
            # 彩虹渐变边框（沿着轮廓变化颜色）
            border_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            border_draw = ImageDraw.Draw(border_layer)

            # 定义彩虹色序列（基于主色调变化）
            rainbow_colors = [
                border_color,  # 主色
                tuple(min(255, c + 40) for c in border_color),  # 亮一点
                (border_color[2], border_color[0], border_color[1]),  # 色相旋转
                (border_color[1], border_color[2], border_color[0]),  # 色相旋转
                border_color,  # 回到主色
            ]

            # 绘制多层渐变边框
            border_width = 4
            for layer in range(border_width):
                alpha = 255 - layer * 30
                perimeter = 2 * (x2 - x1 + y2 - y1)
                step = perimeter // 100  # 分100段

                for i in range(100):
                    # 计算当前位置
                    color_idx = (i * len(rainbow_colors)) // 100
                    next_color_idx = (color_idx + 1) % len(rainbow_colors)
                    local_ratio = ((i * len(rainbow_colors)) % 100) / 100

                    # 颜色插值
                    r = int(rainbow_colors[color_idx][0] + (rainbow_colors[next_color_idx][0] - rainbow_colors[color_idx][0]) * local_ratio)
                    g = int(rainbow_colors[color_idx][1] + (rainbow_colors[next_color_idx][1] - rainbow_colors[color_idx][1]) * local_ratio)
                    b = int(rainbow_colors[color_idx][2] + (rainbow_colors[next_color_idx][2] - rainbow_colors[color_idx][2]) * local_ratio)

                    color = (r, g, b, alpha)

                    # 计算边框上的坐标（沿着矩形轮廓）
                    if i * step < (x2 - x1):  # 顶边
                        px = x1 + i * step
                        py = y1 + layer
                    elif i * step < (x2 - x1 + y2 - y1):  # 右边
                        px = x2 - layer
                        py = y1 + (i * step - (x2 - x1))
                    elif i * step < (2 * (x2 - x1) + y2 - y1):  # 底边
                        px = x2 - (i * step - (x2 - x1 + y2 - y1))
                        py = y2 - layer
                    else:  # 左边
                        px = x1 + layer
                        py = y2 - (i * step - (2 * (x2 - x1) + y2 - y1))

                    border_draw.point((px, py), fill=color)

            # 应用圆角蒙版
            mask = Image.new('L', img.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            # 边框区域蒙版（外圆角 - 内圆角）
            SummaryImageGenerator._draw_rounded_rectangle(
                mask_draw,
                (x1 - 2, y1 - 2, x2 + 2, y2 + 2),
                radius + 2,
                fill=255
            )
            SummaryImageGenerator._draw_rounded_rectangle(
                mask_draw,
                (x1 + 4, y1 + 4, x2 - 4, y2 - 4),
                radius - 4,
                fill=0
            )
            border_layer.putalpha(mask)
            img = Image.alpha_composite(img, border_layer)
        else:
            # 单色边框
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            border_rgba = border_color + (255,)
            SummaryImageGenerator._draw_rounded_rectangle(
                overlay_draw,
                coords,
                radius,
                fill=(0, 0, 0, 0),
                outline=border_rgba,
                width=4
            )
            img = Image.alpha_composite(img, overlay)

        return img

    @staticmethod
    def _draw_text_with_shadow(
        draw: ImageDraw.ImageDraw,
        position: tuple,
        text: str,
        font: ImageFont.FreeTypeFont,
        text_color: tuple,
        shadow_offset: int = 2,
        shadow_color: tuple = (200, 200, 220, 120)
    ):
        """绘制带阴影的文字（柔和版本）"""
        x, y = position

        # 绘制阴影
        draw.text((x + shadow_offset, y + shadow_offset), text, fill=shadow_color, font=font)

        # 绘制主文字
        draw.text((x, y), text, fill=text_color, font=font)

    @staticmethod
    def _draw_colorful_text(
        img: Image.Image,
        position: tuple,
        text: str,
        font: ImageFont.FreeTypeFont,
        text_color: tuple,
        outline_color: tuple = None,
        shadow_radius: int = 6
    ) -> Image.Image:
        """绘制彩色描边文字（明亮风格）"""
        # 创建临时图层
        shadow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)

        # 如果有描边颜色，绘制柔和描边
        if outline_color:
            for offset in range(shadow_radius, 0, -1):
                alpha = int(80 * (shadow_radius - offset) / shadow_radius)
                outline_col = outline_color[:3] + (alpha,)
                for dx in range(-offset, offset + 1):
                    for dy in range(-offset, offset + 1):
                        if dx*dx + dy*dy <= offset*offset:
                            shadow_draw.text(
                                (position[0] + dx, position[1] + dy),
                                text,
                                fill=outline_col,
                                font=font
                            )

            # 应用轻微模糊
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_radius // 3))

            # 合并阴影
            img = img.convert('RGBA')
            img = Image.alpha_composite(img, shadow_layer)

        # 绘制主文字
        text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_layer)
        text_draw.text(position, text, fill=text_color, font=font)
        img = Image.alpha_composite(img, text_layer)

        return img

    @staticmethod
    def _add_decoration_with_glow(
        img: Image.Image,
        deco_path: str,
        position: tuple,
        max_size: tuple,
        glow_color: tuple = None
    ) -> Image.Image:
        """添加带发光效果的装饰图片

        Args:
            img: 目标图片
            deco_path: 装饰图片路径
            position: 位置 (x, y)
            max_size: 最大尺寸 (width, height)
            glow_color: 发光颜色（可选）
        """
        if not os.path.exists(deco_path):
            return img

        try:
            deco_img = Image.open(deco_path).convert("RGBA")
            w, h = deco_img.size

            # 缩放
            scale = min(max_size[0] / w, max_size[1] / h, 1.0)
            new_w, new_h = int(w * scale), int(h * scale)
            if scale < 1.0:
                deco_img = deco_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # 如果有光晕颜色，添加柔和光晕效果
            if glow_color:
                glow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))

                # 创建柔和光晕
                for offset in range(15, 0, -2):
                    alpha = int(40 * (15 - offset) / 15)  # 降低透明度
                    glow_temp = Image.new('RGBA', (new_w + offset * 2, new_h + offset * 2), (0, 0, 0, 0))
                    glow_temp.paste(deco_img, (offset, offset), deco_img)

                    # 添加颜色叠加
                    color_layer = Image.new('RGBA', glow_temp.size, glow_color + (alpha,))
                    glow_temp = Image.alpha_composite(glow_temp, color_layer)

                    glow_layer.paste(glow_temp, (position[0] - offset, position[1] - offset), glow_temp)

                # 应用模糊
                glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=8))
                img = Image.alpha_composite(img, glow_layer)

            # 粘贴装饰图片
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            overlay.paste(deco_img, position, deco_img)
            img = Image.alpha_composite(img, overlay)

            return img

        except Exception as e:
            logger.error(f"添加装饰失败 {deco_path}: {e}")
            return img

    @staticmethod
    def _draw_gradient_badge(
        img: Image.Image,
        position: tuple,
        size: tuple,
        text: str,
        font: ImageFont.FreeTypeFont,
        gradient_start: tuple,
        gradient_end: tuple
    ) -> Image.Image:
        """绘制渐变徽章"""
        x, y = position
        w, h = size

        # 创建临时图层
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)

        # 绘制渐变背景
        SummaryImageGenerator._draw_gradient_rect(
            overlay_draw,
            (x, y, x + w, y + h),
            gradient_start,
            gradient_end,
            horizontal=True
        )

        # 圆角蒙版
        mask = Image.new('L', img.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        SummaryImageGenerator._draw_rounded_rectangle(
            mask_draw,
            (x, y, x + w, y + h),
            h // 2,
            fill=255
        )
        overlay.putalpha(mask)

        # 合并
        img = img.convert('RGBA')
        img = Image.alpha_composite(img, overlay)

        # 绘制文字
        text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_layer)
        text_bbox = font.getbbox(text)
        text_w = text_bbox[2] - text_bbox[0]
        text_h = text_bbox[3] - text_bbox[1]
        text_x = x + (w - text_w) // 2
        text_y = y + (h - text_h) // 2 - 2

        # 文字阴影
        text_draw.text((text_x + 2, text_y + 2), text, fill=(0, 0, 0, 200), font=font)
        text_draw.text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)

        img = Image.alpha_composite(img, text_layer)

        return img

    @staticmethod
    def _draw_decorative_divider(
        img: Image.Image,
        y_position: int,
        width: int,
        padding: int = 60
    ) -> Image.Image:
        """绘制装饰性分隔线 - 带渐变和装饰点

        Args:
            img: 目标图片
            y_position: 分隔线Y坐标
            width: 图片宽度
            padding: 左右边距
        """
        divider_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        divider_draw = ImageDraw.Draw(divider_layer)

        x1 = padding
        x2 = width - padding
        center_x = width // 2

        # 绘制渐变线条（从两端向中间：透明 -> 彩色 -> 透明）
        for i in range(x2 - x1):
            ratio = i / (x2 - x1)
            # 计算透明度（中间高，两端低）
            alpha = int(180 * (1 - abs(2 * ratio - 1)))

            # 彩色渐变（彩虹色）
            hue = (ratio * 360) % 360
            if hue < 60:
                r, g, b = 255, int(hue * 4.25), 180
            elif hue < 120:
                r, g, b = int(255 - (hue - 60) * 4.25), 255, 200
            elif hue < 180:
                r, g, b = 150, 255, int(200 + (hue - 120) * 0.9)
            elif hue < 240:
                r, g, b = 180, int(255 - (hue - 180) * 2), 255
            elif hue < 300:
                r, g, b = int(200 + (hue - 240) * 0.9), 150, 255
            else:
                r, g, b = 255, 160, int(255 - (hue - 300) * 1.25)

            divider_draw.line(
                [(x1 + i, y_position), (x1 + i + 1, y_position)],
                fill=(r, g, b, alpha),
                width=2
            )

        # 添加中心装饰点
        dot_colors = [
            (255, 200, 220, 200),  # 粉
            (200, 220, 255, 200),  # 蓝
            (220, 200, 255, 200),  # 紫
        ]
        dot_positions = [center_x - 20, center_x, center_x + 20]
        for i, pos in enumerate(dot_positions):
            color = dot_colors[i % len(dot_colors)]
            # 外圈光晕
            for r in range(8, 0, -1):
                alpha = int(color[3] * (8 - r) / 8 * 0.3)
                divider_draw.ellipse(
                    [pos - r, y_position - r, pos + r, y_position + r],
                    fill=color[:3] + (alpha,)
                )
            # 实心点
            divider_draw.ellipse(
                [pos - 4, y_position - 4, pos + 4, y_position + 4],
                fill=color
            )

        img = img.convert('RGBA')
        img = Image.alpha_composite(img, divider_layer)
        return img

    @staticmethod
    def _add_corner_decorations(
        img: Image.Image,
        card_rect: tuple,
        corner_path: str,
        color: tuple = None
    ) -> Image.Image:
        """在卡片四角添加装饰

        Args:
            img: 目标图片
            card_rect: 卡片矩形 (x1, y1, x2, y2)
            corner_path: 角落装饰图片路径
            color: 装饰颜色（可选）
        """
        if not os.path.exists(corner_path):
            return img

        try:
            corner_img = Image.open(corner_path).convert("RGBA")
            # 缩放到合适大小
            size = 25
            corner_img = corner_img.resize((size, size), Image.Resampling.LANCZOS)

            x1, y1, x2, y2 = card_rect
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))

            # 左上角
            overlay.paste(corner_img, (x1 + 10, y1 + 10), corner_img)

            # 右上角（水平翻转）
            corner_flip_h = corner_img.transpose(Image.FLIP_LEFT_RIGHT)
            overlay.paste(corner_flip_h, (x2 - size - 10, y1 + 10), corner_flip_h)

            # 左下角（垂直翻转）
            corner_flip_v = corner_img.transpose(Image.FLIP_TOP_BOTTOM)
            overlay.paste(corner_flip_v, (x1 + 10, y2 - size - 10), corner_flip_v)

            # 右下角（水平+垂直翻转）
            corner_flip_both = corner_img.transpose(Image.FLIP_LEFT_RIGHT).transpose(Image.FLIP_TOP_BOTTOM)
            overlay.paste(corner_flip_both, (x2 - size - 10, y2 - size - 10), corner_flip_both)

            img = Image.alpha_composite(img, overlay)
            return img

        except Exception as e:
            logger.error(f"添加角落装饰失败: {e}")
            return img

    # 已删除未使用的方法: _add_scattered_background_decorations
    # 已删除未使用的方法: _draw_stat_badge

    @staticmethod
    def _draw_hourly_chart(
        img: Image.Image,
        coords: tuple,
        hourly_data: dict,
        font: ImageFont.FreeTypeFont
    ) -> Image.Image:
        """绘制24小时发言分布柱状图（带数值标签的圆角柱子）

        Args:
            img: 目标图片
            coords: 图表区域坐标 (x1, y1, x2, y2)
            hourly_data: 24小时发言数据 {hour: count}
            font: 字体

        Returns:
            绘制后的图片
        """
        x1, y1, x2, y2 = coords
        chart_width = x2 - x1
        chart_height = y2 - y1

        # 创建图层
        chart_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        chart_draw = ImageDraw.Draw(chart_layer)

        # 计算柱状图参数
        bar_count = 24
        bar_spacing = 10  # 增加间距从8到10
        total_spacing = bar_spacing * (bar_count - 1)
        bar_width = (chart_width - total_spacing) // bar_count

        # 获取最大值用于缩放
        max_count = max(hourly_data.values()) if hourly_data else 1
        if max_count == 0:
            max_count = 1

        # 绘制区域高度（留出顶部空间给数值标签，底部空间给时间标签）
        label_height = 35
        value_label_space = 40  # 顶部预留空间显示数值
        available_height = chart_height - label_height - value_label_space - 20

        # 绘制每个柱子
        for hour in range(24):
            count = hourly_data.get(hour, 0)

            # 计算柱子高度（至少显示3像素，便于看到圆角）
            bar_height = max(3, int(available_height * count / max_count)) if max_count > 0 else 3

            # 计算柱子位置
            bar_x = x1 + hour * (bar_width + bar_spacing)
            bar_y = y1 + value_label_space + available_height - bar_height

            # 渐变色彩 - 根据时间段选择颜色
            if 0 <= hour < 6:  # 深夜 - 深蓝紫
                color_start = (120, 100, 200)
                color_end = (80, 60, 160)
            elif 6 <= hour < 12:  # 早晨 - 橙黄
                color_start = (255, 200, 100)
                color_end = (255, 160, 80)
            elif 12 <= hour < 18:  # 下午 - 青蓝
                color_start = (100, 200, 255)
                color_end = (80, 160, 220)
            else:  # 晚上 - 粉紫
                color_start = (255, 150, 200)
                color_end = (220, 100, 180)

            # 圆角半径
            corner_radius = min(bar_width // 2, 8)

            # 创建柱子的渐变填充
            # 先绘制矩形主体
            for i in range(bar_height):
                ratio = i / max(1, bar_height)
                r = int(color_start[0] + (color_end[0] - color_start[0]) * ratio)
                g = int(color_start[1] + (color_end[1] - color_start[1]) * ratio)
                b = int(color_start[2] + (color_end[2] - color_start[2]) * ratio)

                line_y = bar_y + bar_height - i - 1

                # 如果在顶部圆角区域，使用圆角绘制
                if i < corner_radius:
                    # 计算圆角裁剪
                    for px in range(bar_width):
                        # 检查是否在圆角范围内
                        left_corner_dist = ((px - corner_radius) ** 2 + (i - corner_radius) ** 2) ** 0.5
                        right_corner_dist = ((px - (bar_width - corner_radius)) ** 2 + (i - corner_radius) ** 2) ** 0.5

                        if px < corner_radius:  # 左上角
                            if left_corner_dist <= corner_radius:
                                chart_draw.point((bar_x + px, line_y), fill=(r, g, b, 240))
                        elif px >= bar_width - corner_radius:  # 右上角
                            if right_corner_dist <= corner_radius:
                                chart_draw.point((bar_x + px, line_y), fill=(r, g, b, 240))
                        else:  # 中间部分
                            chart_draw.point((bar_x + px, line_y), fill=(r, g, b, 240))
                else:
                    # 非圆角部分，直接绘制线条
                    chart_draw.line(
                        [(bar_x, line_y), (bar_x + bar_width, line_y)],
                        fill=(r, g, b, 240)
                    )

            # 在柱子顶部显示消息数量（只显示大于0的）
            if count > 0:
                count_text = str(count)
                count_bbox = font.getbbox(count_text)
                count_w = count_bbox[2] - count_bbox[0]
                count_h = count_bbox[3] - count_bbox[1]

                # 数值标签位置（柱子正上方）
                count_x = bar_x + (bar_width - count_w) // 2
                count_y = bar_y - count_h - 8

                # 绘制数字阴影（增强可读性）
                chart_draw.text(
                    (count_x + 1, count_y + 1),
                    count_text,
                    fill=(0, 0, 0, 200),
                    font=font
                )

                # 绘制数字（使用渐变色系中的明亮色）
                chart_draw.text(
                    (count_x, count_y),
                    count_text,
                    fill=(100, 200, 255, 255),  # 青色，与图表配色一致
                    font=font
                )

            # 绘制时间标签（每4小时显示一次）
            if hour % 4 == 0:
                label_text = f"{hour:02d}"
                label_bbox = font.getbbox(label_text)
                label_w = label_bbox[2] - label_bbox[0]
                label_x = bar_x + (bar_width - label_w) // 2
                label_y = y1 + value_label_space + available_height + 10

                chart_draw.text(
                    (label_x, label_y),
                    label_text,
                    fill=SummaryImageGenerator.LIGHT_TEXT_COLOR + (255,),
                    font=font
                )

        # 合并图层
        img = Image.alpha_composite(img, chart_layer)

        return img

    @staticmethod
    async def generate_summary_image(
        title: str,
        summary_text: str,
        time_info: str = "",
        message_count: int = 0,
        participant_count: int = 0,
        width: int = None,
        user_titles: list = None,
        golden_quotes: list = None,
        depression_index: list = None,
        hourly_distribution: dict = None,
        user_profile: dict = None
    ) -> str:
        """生成聊天总结图片 - 霓虹赛博朋克风格

        Args:
            title: 标题
            summary_text: 总结文本
            time_info: 时间信息
            message_count: 消息数量
            participant_count: 参与人数
            width: 图片宽度
            user_titles: 群友称号列表
            golden_quotes: 金句列表
            depression_index: 炫压抑指数列表
            hourly_distribution: 24小时发言分布数据 {hour: count}
            user_profile: 单个用户画像数据 {tags, active_time, fun_score, fun_comment, topic_leadership, topic_comment, rank_title, rank_desc, mood, mood_score, mood_reason}

        Returns:
            str: 临时图片文件的绝对路径
        """
        if width is None:
            width = SummaryImageGenerator.WIDTH

        # 初始化
        if user_titles is None:
            user_titles = []
        if golden_quotes is None:
            golden_quotes = []
        if hourly_distribution is None:
            hourly_distribution = {}

        # 加载字体
        font_title = SummaryImageGenerator._get_font(SummaryImageGenerator.TITLE_SIZE)
        font_section_title = SummaryImageGenerator._get_font(SummaryImageGenerator.SECTION_TITLE_SIZE)
        font_subtitle = SummaryImageGenerator._get_font(SummaryImageGenerator.SUBTITLE_SIZE)
        font_text = SummaryImageGenerator._get_font(SummaryImageGenerator.TEXT_SIZE)
        font_small = SummaryImageGenerator._get_font(SummaryImageGenerator.SMALL_SIZE)

        # 获取插件根目录（core的父目录）
        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # ===== 计算所需高度 =====
        header_height = 300
        hourly_chart_height = 0
        summary_card_height = 0
        titles_section_height = 0
        quotes_section_height = 0
        user_profile_height = 0

        # 如果是用户画像模式，计算用户画像相关区域高度
        if user_profile:
            # 分隔线区域
            user_profile_height += 50

            # 动态计算三个指标区域的高度
            # 每个指标：标题行(35) + 评价文本(约40，按2行算) + 间距(20) = 95
            # 三个指标：95 * 3 = 285
            # 加上分隔线和间距：285 + 30 = 315
            indicators_height = 315

            # 整体卡片高度：顶部信息区(155，增加了心情理由间距) + 三个指标区域(315) + 底部间距(20)
            total_card_height = 155 + indicators_height + 20
            user_profile_height += total_card_height + 30  # 卡片 + 间距

        # 计算24小时分布图表高度
        if hourly_distribution and any(hourly_distribution.values()):
            hourly_chart_height = 440  # 分隔线40 + 标题区100 + 图表250 + 间距50

        # 计算总结卡片高度（优化行间距从15到18）
        max_text_width = width - SummaryImageGenerator.PADDING * 2 - SummaryImageGenerator.CARD_PADDING * 2
        wrapped_lines = SummaryImageGenerator._wrap_text(summary_text, max_text_width, font_text)
        line_height = font_text.getbbox('测试')[3] - font_text.getbbox('测试')[1]
        # 总结卡片区域 = 分隔线40 + 卡片本身 + 间距50
        card_content_height = SummaryImageGenerator.CARD_PADDING * 2 + len(wrapped_lines) * (line_height + 18) + 80
        summary_card_height = 40 + card_content_height + 50

        # 计算称号区域高度
        if user_titles:
            titles_section_height = 190  # 分隔线40 + 标题区150
            max_reason_width = width - SummaryImageGenerator.PADDING * 2 - SummaryImageGenerator.CARD_PADDING * 2
            reason_line_height = font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1]
            title_line_height = font_subtitle.getbbox('测试')[3] - font_subtitle.getbbox('测试')[1]
            for title_item in user_titles[:4]:  # 显示4个
                reason = title_item.get("reason", "")
                reason_lines = SummaryImageGenerator._wrap_text(reason, max_reason_width, font_small)
                card_height = 50 + title_line_height + 25 + len(reason_lines) * (reason_line_height + 8) + 30
                card_height = max(card_height, 120)
                titles_section_height += card_height + SummaryImageGenerator.CARD_SPACING
            titles_section_height += 30  # 区域底部间距

        # 计算金句区域高度
        if golden_quotes:
            quotes_section_height = 190  # 分隔线40 + 标题区150
            max_quote_width = width - SummaryImageGenerator.PADDING * 2 - SummaryImageGenerator.CARD_PADDING * 2
            reason_line_height = font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1]
            for quote_item in golden_quotes[:4]:  # 显示4个
                content = quote_item.get("content", "")
                reason = quote_item.get("reason", "")
                quote_text = f'"{content}"'
                quote_lines = SummaryImageGenerator._wrap_text(quote_text, max_quote_width, font_text)
                reason_lines = SummaryImageGenerator._wrap_text(reason, max_quote_width, font_small)
                card_height = 50 + len(quote_lines) * (line_height + 12) + 50 + len(reason_lines) * (reason_line_height + 8) + 40
                card_height = max(card_height, 200)
                quotes_section_height += card_height + SummaryImageGenerator.CARD_SPACING

        # 计算炫压抑指数区域高度
        depression_index_height = 0
        if depression_index:
            depression_index_height = 190  # 分隔线40 + 标题区150
            card_h = 160  # 每个卡片高度（增加以容纳文言文评价）
            num_rows = (len(depression_index[:4]) + 1) // 2  # 最多4个，2列布局，计算行数
            depression_index_height += num_rows * (card_h + 25) + 10  # 每行高度+间距

        # 总高度（底部装饰区域）
        footer_height = 160  # 减少底部空白
        total_height = header_height + hourly_chart_height + summary_card_height + titles_section_height + quotes_section_height + depression_index_height + user_profile_height + footer_height

        # ===== 创建图片 =====
        img = Image.new('RGB', (width, total_height), SummaryImageGenerator.BG_START)
        draw = ImageDraw.Draw(img)

        # 绘制渐变背景
        for y in range(total_height):
            if y < total_height // 2:
                ratio = y / (total_height // 2)
                r = int(SummaryImageGenerator.BG_START[0] + (SummaryImageGenerator.BG_MID[0] - SummaryImageGenerator.BG_START[0]) * ratio)
                g = int(SummaryImageGenerator.BG_START[1] + (SummaryImageGenerator.BG_MID[1] - SummaryImageGenerator.BG_START[1]) * ratio)
                b = int(SummaryImageGenerator.BG_START[2] + (SummaryImageGenerator.BG_MID[2] - SummaryImageGenerator.BG_START[2]) * ratio)
            else:
                ratio = (y - total_height // 2) / (total_height // 2)
                r = int(SummaryImageGenerator.BG_MID[0] + (SummaryImageGenerator.BG_END[0] - SummaryImageGenerator.BG_MID[0]) * ratio)
                g = int(SummaryImageGenerator.BG_MID[1] + (SummaryImageGenerator.BG_END[1] - SummaryImageGenerator.BG_MID[1]) * ratio)
                b = int(SummaryImageGenerator.BG_MID[2] + (SummaryImageGenerator.BG_END[2] - SummaryImageGenerator.BG_MID[2]) * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

        # 转换为RGBA
        img = img.convert('RGBA')

        # 增强背景装饰 - 波点 + 流动光线 + 星星粒子
        import random
        random.seed(42)  # 固定种子保证每次生成相同图案
        bg_overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        bg_draw = ImageDraw.Draw(bg_overlay)

        # 1. 绘制柔和波点
        for _ in range(80):
            x = random.randint(0, width)
            y = random.randint(0, total_height)
            size = random.randint(30, 80)
            colors = [
                (255, 200, 220, 25),  # 粉色
                (200, 220, 255, 25),  # 蓝色
                (220, 200, 255, 25),  # 紫色
                (255, 240, 200, 25),  # 金色
            ]
            color = random.choice(colors)
            bg_draw.ellipse([x, y, x + size, y + size], fill=color)

        # 2. 添加流动光线（斜向光束）
        for i in range(5):
            start_x = random.randint(-200, width)
            start_y = i * (total_height // 5)
            line_length = random.randint(400, 800)

            # 绘制渐变光束
            for step in range(line_length):
                ratio = step / line_length
                # 光束透明度（中间亮，两端暗）
                alpha = int(50 * (1 - abs(2 * ratio - 1)))

                # 光束颜色（随机选择）
                beam_colors = [
                    (200, 220, 255),  # 蓝色
                    (255, 200, 220),  # 粉色
                    (220, 200, 255),  # 紫色
                ]
                beam_color = beam_colors[i % len(beam_colors)]

                x = start_x + step
                y = start_y + step * 0.3  # 斜向

                if 0 <= x < width and 0 <= y < total_height:
                    # 绘制光束点（带渐变宽度）
                    beam_width = int(3 * (1 - abs(2 * ratio - 1)))
                    for w in range(-beam_width, beam_width + 1):
                        draw_y = int(y + w)
                        if 0 <= draw_y < total_height:
                            pixel_alpha = int(alpha * (1 - abs(w) / max(1, beam_width)))
                            bg_draw.point((int(x), draw_y), fill=beam_color + (pixel_alpha,))

        # 3. 添加闪烁星星粒子
        for _ in range(120):
            star_x = random.randint(0, width)
            star_y = random.randint(0, total_height)
            star_size = random.choice([1, 2, 3])  # 不同大小的星星

            # 星星颜色（柔和亮色）
            star_colors = [
                (255, 255, 220, 180),  # 金色
                (220, 240, 255, 180),  # 浅蓝
                (255, 230, 240, 180),  # 粉白
                (240, 230, 255, 180),  # 淡紫
            ]
            star_color = random.choice(star_colors)

            if star_size == 1:
                # 小星星：单点
                bg_draw.point((star_x, star_y), fill=star_color)
            elif star_size == 2:
                # 中星星：十字形
                for dx, dy in [(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)]:
                    nx, ny = star_x + dx, star_y + dy
                    if 0 <= nx < width and 0 <= ny < total_height:
                        alpha = star_color[3] if dx == 0 and dy == 0 else star_color[3] // 2
                        bg_draw.point((nx, ny), fill=star_color[:3] + (alpha,))
            else:
                # 大星星：带光晕的十字
                for r in range(3, 0, -1):
                    alpha = int(star_color[3] * (3 - r) / 3 * 0.6)
                    for dx, dy in [(0, r), (0, -r), (r, 0), (-r, 0)]:
                        nx, ny = star_x + dx, star_y + dy
                        if 0 <= nx < width and 0 <= ny < total_height:
                            bg_draw.point((nx, ny), fill=star_color[:3] + (alpha,))
                # 中心点
                bg_draw.point((star_x, star_y), fill=star_color)

        img = Image.alpha_composite(img, bg_overlay)

        # 已删除：散落的装饰图标到背景
        # img = SummaryImageGenerator._add_scattered_background_decorations(
        #     img,
        #     plugin_dir,
        #     seed=42  # 使用固定种子保证每次生成位置一致
        # )

        y = 0

        # ===== 标题区域 =====
        title_clean = title.replace('📊', '').strip()

        title_bbox = font_title.getbbox(title_clean)
        title_width = title_bbox[2] - title_bbox[0]
        title_x = (width - title_width) // 2
        title_y = 80

        # 绘制彩色描边标题
        img = SummaryImageGenerator._draw_colorful_text(
            img,
            (title_x, title_y),
            title_clean,
            font_title,
            SummaryImageGenerator.TITLE_COLOR,
            outline_color=SummaryImageGenerator.BORDER_PURPLE,
            shadow_radius=8
        )

        # 添加decoration1装饰（标题左侧）
        deco1_path = os.path.join(plugin_dir, "decorations", "decoration1.png")
        img = SummaryImageGenerator._add_decoration_with_glow(
            img,
            deco1_path,
            (title_x - 200, title_y - 30),
            (150, 150),
            SummaryImageGenerator.BORDER_CYAN
        )

        # 右侧镜像
        if os.path.exists(deco1_path):
            try:
                deco1_img = Image.open(deco1_path).convert("RGBA")
                w, h = deco1_img.size
                scale = min(150 / w, 150 / h, 1.0)
                new_w, new_h = int(w * scale), int(h * scale)
                if scale < 1.0:
                    deco1_img = deco1_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                # 镜像翻转
                deco1_flipped = deco1_img.transpose(Image.FLIP_LEFT_RIGHT)

                # 手动添加光晕和图片（使用镜像后的图片）
                paste_x = title_x + title_width + 50
                paste_y = title_y - 30

                # 添加柔和光晕
                glow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                for offset in range(15, 0, -2):
                    alpha = int(40 * (15 - offset) / 15)
                    glow_temp = Image.new('RGBA', (new_w + offset * 2, new_h + offset * 2), (0, 0, 0, 0))
                    glow_temp.paste(deco1_flipped, (offset, offset), deco1_flipped)
                    color_layer = Image.new('RGBA', glow_temp.size, SummaryImageGenerator.BORDER_CYAN + (alpha,))
                    glow_temp = Image.alpha_composite(glow_temp, color_layer)
                    glow_layer.paste(glow_temp, (paste_x - offset, paste_y - offset), glow_temp)

                glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=8))
                img = Image.alpha_composite(img, glow_layer)

                # 粘贴镜像图片
                overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                overlay.paste(deco1_flipped, (paste_x, paste_y), deco1_flipped)
                img = Image.alpha_composite(img, overlay)
            except Exception as e:
                logger.error(f"添加镜像decoration1失败: {e}")

        # 添加星星装饰
        star_path = os.path.join(plugin_dir, "decorations", "decoration_star.png")
        positions = [
            (title_x - 280, 60),
            (title_x + title_width + 250, 70),
            (title_x - 320, 140),
            (title_x + title_width + 290, 150),
        ]
        for pos in positions:
            img = SummaryImageGenerator._add_decoration_with_glow(
                img,
                star_path,
                pos,
                (40, 40),
                SummaryImageGenerator.BORDER_YELLOW
            )

        # 绘制时间和统计信息 - 单个统一徽章
        badge_y = 200

        # 构建统计信息文本
        stats_parts = []
        if time_info:
            stats_parts.append(time_info)
        if message_count > 0:
            stats_parts.append(f"{message_count}条消息")
        if participant_count > 0:
            stats_parts.append(f"{participant_count}人参与")

        if stats_parts:
            stats_text = " 丨 ".join(stats_parts)

            # 计算徽章尺寸
            stats_bbox = font_small.getbbox(stats_text)
            stats_w = stats_bbox[2] - stats_bbox[0]
            stats_h = stats_bbox[3] - stats_bbox[1]

            badge_w = stats_w + 60
            badge_h = stats_h + 20
            badge_radius = badge_h // 2
            badge_x = (width - badge_w) // 2

            # 创建独立的徽章图层
            badge_img = Image.new('RGBA', (badge_w, badge_h), (0, 0, 0, 0))
            badge_draw = ImageDraw.Draw(badge_img)

            # 绘制徽章背景（渐变）
            for i in range(badge_w):
                ratio = i / badge_w
                r = int(SummaryImageGenerator.GRADIENT_1_START[0] + (SummaryImageGenerator.GRADIENT_2_END[0] - SummaryImageGenerator.GRADIENT_1_START[0]) * ratio)
                g = int(SummaryImageGenerator.GRADIENT_1_START[1] + (SummaryImageGenerator.GRADIENT_2_END[1] - SummaryImageGenerator.GRADIENT_1_START[1]) * ratio)
                b = int(SummaryImageGenerator.GRADIENT_1_START[2] + (SummaryImageGenerator.GRADIENT_2_END[2] - SummaryImageGenerator.GRADIENT_1_START[2]) * ratio)
                badge_draw.line(
                    [(i, 0), (i, badge_h)],
                    fill=(r, g, b, 230)
                )

            # 应用圆角蒙版
            mask = Image.new('L', (badge_w, badge_h), 0)
            mask_draw = ImageDraw.Draw(mask)
            SummaryImageGenerator._draw_rounded_rectangle(
                mask_draw,
                (0, 0, badge_w, badge_h),
                badge_radius,
                fill=255
            )
            badge_img.putalpha(mask)

            # 绘制文本到徽章
            text_draw = ImageDraw.Draw(badge_img)
            text_x = (badge_w - stats_w) // 2
            text_y = (badge_h - stats_h) // 2

            # 绘制文本（深色字体，更清晰）
            text_draw.text(
                (text_x, text_y),
                stats_text,
                font=font_small,
                fill=(60, 60, 80, 255)  # 使用深色字体
            )

            # 将徽章合成到主图
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            overlay.paste(badge_img, (badge_x, badge_y), badge_img)
            img = Image.alpha_composite(img, overlay)

        y = header_height

        # ===== 24小时发言分布图表 =====
        if hourly_distribution and any(hourly_distribution.values()):
            # 添加装饰性分隔线
            img = SummaryImageGenerator._draw_decorative_divider(img, y + 10, width)
            y += 40

            # 标题
            section_title = "24小时发言分布"
            title_bbox = font_section_title.getbbox(section_title)
            section_title_width = title_bbox[2] - title_bbox[0]
            section_title_x = (width - section_title_width) // 2

            # 彩色描边标题
            img = SummaryImageGenerator._draw_colorful_text(
                img,
                (section_title_x, y + 20),
                section_title,
                font_section_title,
                SummaryImageGenerator.TITLE_COLOR,
                outline_color=SummaryImageGenerator.BORDER_GREEN,
                shadow_radius=8
            )

            y += 100

            # 绘制图表卡片（增加高度以容纳顶部数值标签）
            card_x = SummaryImageGenerator.PADDING
            card_width = width - SummaryImageGenerator.PADDING * 2
            chart_height = 250  # 从200增加到250，为数值标签预留空间

            img = SummaryImageGenerator._draw_colorful_card(
                img,
                (card_x, y, card_x + card_width, y + chart_height),
                SummaryImageGenerator.BORDER_GREEN,
                radius=25,
                shadow_strength=15
            )

            # 添加角落装饰
            corner_path = os.path.join(plugin_dir, "decorations", "decoration_corner.png")
            img = SummaryImageGenerator._add_corner_decorations(
                img,
                (card_x, y, card_x + card_width, y + chart_height),
                corner_path,
                SummaryImageGenerator.BORDER_GREEN
            )

            # 绘制图表
            chart_x1 = card_x + SummaryImageGenerator.CARD_PADDING
            chart_y1 = y + SummaryImageGenerator.CARD_PADDING
            chart_x2 = card_x + card_width - SummaryImageGenerator.CARD_PADDING
            chart_y2 = y + chart_height - SummaryImageGenerator.CARD_PADDING

            img = SummaryImageGenerator._draw_hourly_chart(
                img,
                (chart_x1, chart_y1, chart_x2, chart_y2),
                hourly_distribution,
                font_small
            )

            y += chart_height + 50

        # 添加装饰性分隔线
        img = SummaryImageGenerator._draw_decorative_divider(img, y + 10, width)
        y += 40

        # ===== 总结卡片（霓虹卡片） =====
        card_x = SummaryImageGenerator.PADDING
        card_width = width - SummaryImageGenerator.PADDING * 2

        img = SummaryImageGenerator._draw_colorful_card(
            img,
            (card_x, y, card_x + card_width, y + card_content_height),
            SummaryImageGenerator.BORDER_CYAN,
            radius=25,
            shadow_strength=15
        )

        # 添加角落装饰
        corner_path = os.path.join(plugin_dir, "decorations", "decoration_corner.png")
        img = SummaryImageGenerator._add_corner_decorations(
            img,
            (card_x, y, card_x + card_width, y + card_content_height),
            corner_path,
            SummaryImageGenerator.BORDER_CYAN
        )

        # 绘制总结文本（优化行间距从15到18）
        text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_layer)
        text_y = y + SummaryImageGenerator.CARD_PADDING + 20
        text_x = card_x + SummaryImageGenerator.CARD_PADDING

        for line in wrapped_lines:
            if line:
                SummaryImageGenerator._draw_text_with_shadow(
                    text_draw,
                    (text_x, text_y),
                    line,
                    font_text,
                    SummaryImageGenerator.TEXT_COLOR,
                    shadow_offset=2
                )
            text_y += line_height + 18  # 优化行间距从15到18

        img = Image.alpha_composite(img, text_layer)

        # 在总结卡片角落添加闪光装饰
        sparkle_path = os.path.join(plugin_dir, "decorations", "decoration_sparkle.png")
        sparkle_positions = [
            (card_x + 15, y + 15),
            (card_x + card_width - 55, y + 15),
            (card_x + 15, y + card_content_height - 55),
            (card_x + card_width - 55, y + card_content_height - 55),
        ]
        for pos in sparkle_positions:
            img = SummaryImageGenerator._add_decoration_with_glow(
                img,
                sparkle_path,
                pos,
                (40, 40),
                SummaryImageGenerator.BORDER_CYAN
            )

        y += card_content_height + 50

        # ===== 群友称号区域 =====
        if user_titles:
            # 添加装饰性分隔线
            img = SummaryImageGenerator._draw_decorative_divider(img, y + 10, width)
            y += 40

            # 标题
            section_title = "群友称号"
            title_bbox = font_section_title.getbbox(section_title)
            section_title_width = title_bbox[2] - title_bbox[0]
            section_title_x = (width - section_title_width) // 2

            # 彩色描边标题
            img = SummaryImageGenerator._draw_colorful_text(
                img,
                (section_title_x, y + 30),
                section_title,
                font_section_title,
                SummaryImageGenerator.TITLE_COLOR,
                outline_color=SummaryImageGenerator.BORDER_MAGENTA,
                shadow_radius=8
            )

            # 添加decoration3装饰（群友称号区域）
            deco3_path = os.path.join(plugin_dir, "decorations", "decoration3.png")
            img = SummaryImageGenerator._add_decoration_with_glow(
                img,
                deco3_path,
                (section_title_x - 150, y + 10),
                (120, 120),
                SummaryImageGenerator.BORDER_MAGENTA
            )

            # 右侧镜像
            if os.path.exists(deco3_path):
                try:
                    deco3_img = Image.open(deco3_path).convert("RGBA")
                    w, h = deco3_img.size
                    scale = min(120 / w, 120 / h, 1.0)
                    new_w, new_h = int(w * scale), int(h * scale)
                    if scale < 1.0:
                        deco3_img = deco3_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                    deco3_flipped = deco3_img.transpose(Image.FLIP_LEFT_RIGHT)
                    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    paste_x = section_title_x + section_title_width + 30
                    paste_y = y + 10

                    # 添加柔和光晕
                    glow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    for offset in range(15, 0, -2):
                        alpha = int(40 * (15 - offset) / 15)
                        glow_temp = Image.new('RGBA', (new_w + offset * 2, new_h + offset * 2), (0, 0, 0, 0))
                        glow_temp.paste(deco3_flipped, (offset, offset), deco3_flipped)
                        color_layer = Image.new('RGBA', glow_temp.size, SummaryImageGenerator.BORDER_MAGENTA + (alpha,))
                        glow_temp = Image.alpha_composite(glow_temp, color_layer)
                        glow_layer.paste(glow_temp, (paste_x - offset, paste_y - offset), glow_temp)

                    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=8))
                    img = Image.alpha_composite(img, glow_layer)

                    overlay.paste(deco3_flipped, (paste_x, paste_y), deco3_flipped)
                    img = Image.alpha_composite(img, overlay)
                except Exception as e:
                    logger.error(f"添加镜像decoration3失败: {e}")

            y += 150

            # 称号卡片
            badge_colors = [
                (SummaryImageGenerator.GRADIENT_3_START, SummaryImageGenerator.GRADIENT_3_END, SummaryImageGenerator.BORDER_YELLOW),   # 金色
                (SummaryImageGenerator.GRADIENT_1_START, SummaryImageGenerator.GRADIENT_1_END, SummaryImageGenerator.BORDER_CYAN),     # 青色
                (SummaryImageGenerator.GRADIENT_2_START, SummaryImageGenerator.GRADIENT_2_END, SummaryImageGenerator.BORDER_MAGENTA),  # 品红
                (SummaryImageGenerator.GRADIENT_1_START, SummaryImageGenerator.GRADIENT_1_END, SummaryImageGenerator.BORDER_PURPLE),   # 紫色（第4个）
            ]

            for idx, title_item in enumerate(user_titles[:4]):
                name = title_item.get("name", "")
                title_text = title_item.get("title", "")
                reason = title_item.get("reason", "")
                user_id = title_item.get("user_id", "")  # 获取 user_id

                # 计算称号徽章的实际宽度
                title_bbox = font_subtitle.getbbox(title_text)
                title_w = title_bbox[2] - title_bbox[0]
                badge_w = title_w + 30

                # 计算理由区域的可用宽度
                # 布局: [PADDING] + [头像100] + [间距20] + [称号badge_w] + [间距40] + [理由...] + [PADDING]
                avatar_and_spacing = 100 + 20 if user_id else 0  # 有头像才占用
                badge_and_spacing = badge_w + 40  # 称号徽章 + 右侧间距

                max_reason_width = (card_width - SummaryImageGenerator.CARD_PADDING * 2
                                   - avatar_and_spacing - badge_and_spacing)
                # 确保最小宽度,防止过窄
                max_reason_width = max(max_reason_width, 200)

                reason_lines = SummaryImageGenerator._wrap_text(reason, max_reason_width, font_small)
                reason_line_height = font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1]
                title_line_height = font_subtitle.getbbox('测试')[3] - font_subtitle.getbbox('测试')[1]

                # 新布局：卡片高度由头像高度和内容高度中的较大值决定
                avatar_size = 100  # 头像尺寸
                content_height = title_line_height + 8 + title_line_height + 20  # 称号 + 间距 + 名字 + 边距
                reason_height = len(reason_lines) * (reason_line_height + 8) if reason_lines else 0

                card_height = max(avatar_size + 60, content_height + 40, reason_height + 60)  # 60是上下边距
                card_height = max(card_height, 160)  # 最小高度(增大以适应更大的头像)

                # 彩色卡片
                grad_start, grad_end, border_color = badge_colors[idx]
                img = SummaryImageGenerator._draw_colorful_card(
                    img,
                    (card_x, y, card_x + card_width, y + card_height),
                    border_color,
                    radius=20,
                    shadow_strength=15
                )

                # 添加角落装饰
                corner_path = os.path.join(plugin_dir, "decorations", "decoration_corner.png")
                img = SummaryImageGenerator._add_corner_decorations(
                    img,
                    (card_x, y, card_x + card_width, y + card_height),
                    corner_path,
                    border_color
                )

                # 新布局：左边头像 + 中间称号/名字(上下排列) + 右边理由
                content_x = card_x + SummaryImageGenerator.CARD_PADDING
                content_y = y + 30

                # 0. 添加用户头像（在最左边，放大）
                avatar_size = 100  # 头像尺寸
                avatar_added = False
                if user_id:
                    try:
                        avatar = await SummaryImageGenerator._download_qq_avatar(user_id, size=640)
                        if avatar:
                            # 创建圆形头像
                            circular_avatar = SummaryImageGenerator._create_circular_avatar(avatar, avatar_size)

                            # 添加带光晕的头像
                            avatar_x = content_x
                            avatar_y = content_y
                            img = SummaryImageGenerator._add_avatar_glow(
                                img,
                                circular_avatar,
                                (avatar_x, avatar_y),
                                border_color,
                                glow_radius=10
                            )
                            avatar_added = True
                    except Exception as e:
                        logger.error(f"添加用户头像失败 (QQ:{user_id}): {e}")

                # 中间区域起始位置（头像右侧）
                middle_x = content_x + avatar_size + 20 if avatar_added else content_x
                middle_y = content_y

                # 1. 绘制群称号徽章（在上方）
                # title_w 和 badge_w 已经在前面计算过了
                badge_h = title_line_height + 16
                badge_x = middle_x
                badge_y = middle_y

                img = SummaryImageGenerator._draw_gradient_badge(
                    img,
                    (badge_x, badge_y),
                    (badge_w, badge_h),
                    title_text,
                    font_subtitle,
                    grad_start,
                    grad_end
                )

                # 2. 绘制群友名称（在称号下方）
                text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                text_draw = ImageDraw.Draw(text_layer)

                name_x = badge_x + 8  # 与称号对齐
                name_y = badge_y + badge_h + 8  # 称号下方

                # 群友名称（加粗效果）
                for offset_x in [0, 1]:
                    for offset_y in [0, 1]:
                        text_draw.text(
                            (name_x + offset_x, name_y + offset_y),
                            name,
                            fill=SummaryImageGenerator.TITLE_COLOR,
                            font=font_subtitle
                        )

                # 3. 右侧：理由（垂直居中对齐）
                reason_x = badge_x + badge_w + 40  # 称号右侧留空隙

                # 计算理由文本总高度,使其垂直居中
                total_reason_height = len(reason_lines) * reason_line_height + (len(reason_lines) - 1) * 8 if len(reason_lines) > 0 else 0
                reason_y = content_y + (avatar_size - total_reason_height) // 2 if avatar_added else middle_y + 10

                for line in reason_lines:
                    SummaryImageGenerator._draw_text_with_shadow(
                        text_draw,
                        (reason_x, reason_y),
                        line,
                        font_small,
                        SummaryImageGenerator.LIGHT_TEXT_COLOR,
                        shadow_offset=1
                    )
                    reason_y += reason_line_height + 8

                img = Image.alpha_composite(img, text_layer)

                y += card_height + SummaryImageGenerator.CARD_SPACING

            y += 30

        # ===== 金句区域 =====
        if golden_quotes:
            # 添加装饰性分隔线
            img = SummaryImageGenerator._draw_decorative_divider(img, y + 10, width)
            y += 40

            # 标题
            section_title = "群圣经"
            title_bbox = font_section_title.getbbox(section_title)
            section_title_width = title_bbox[2] - title_bbox[0]
            section_title_x = (width - section_title_width) // 2

            # 彩色描边标题
            img = SummaryImageGenerator._draw_colorful_text(
                img,
                (section_title_x, y + 30),
                section_title,
                font_section_title,
                SummaryImageGenerator.TITLE_COLOR,
                outline_color=SummaryImageGenerator.BORDER_ORANGE,
                shadow_radius=8
            )

            # 添加decoration4装饰（金句区域）
            deco4_path = os.path.join(plugin_dir, "decorations", "decoration4.png")
            img = SummaryImageGenerator._add_decoration_with_glow(
                img,
                deco4_path,
                (section_title_x - 150, y + 10),
                (120, 120),
                SummaryImageGenerator.BORDER_ORANGE
            )

            # 右侧镜像
            if os.path.exists(deco4_path):
                try:
                    deco4_img = Image.open(deco4_path).convert("RGBA")
                    w, h = deco4_img.size
                    scale = min(120 / w, 120 / h, 1.0)
                    new_w, new_h = int(w * scale), int(h * scale)
                    if scale < 1.0:
                        deco4_img = deco4_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                    deco4_flipped = deco4_img.transpose(Image.FLIP_LEFT_RIGHT)
                    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    paste_x = section_title_x + section_title_width + 30
                    paste_y = y + 10

                    # 添加柔和光晕
                    glow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    for offset in range(15, 0, -2):
                        alpha = int(40 * (15 - offset) / 15)
                        glow_temp = Image.new('RGBA', (new_w + offset * 2, new_h + offset * 2), (0, 0, 0, 0))
                        glow_temp.paste(deco4_flipped, (offset, offset), deco4_flipped)
                        color_layer = Image.new('RGBA', glow_temp.size, SummaryImageGenerator.BORDER_ORANGE + (alpha,))
                        glow_temp = Image.alpha_composite(glow_temp, color_layer)
                        glow_layer.paste(glow_temp, (paste_x - offset, paste_y - offset), glow_temp)

                    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=8))
                    img = Image.alpha_composite(img, glow_layer)

                    overlay.paste(deco4_flipped, (paste_x, paste_y), deco4_flipped)
                    img = Image.alpha_composite(img, overlay)
                except Exception as e:
                    logger.error(f"添加镜像decoration4失败: {e}")

            # 添加引号装饰
            quote_deco_path = os.path.join(plugin_dir, "decorations", "decoration_quote.png")
            img = SummaryImageGenerator._add_decoration_with_glow(
                img,
                quote_deco_path,
                (section_title_x - 80, y + 35),
                (50, 50),
                SummaryImageGenerator.BORDER_ORANGE
            )

            y += 150

            # 金句卡片
            for idx, quote_item in enumerate(golden_quotes[:4]):
                content = quote_item.get("content", "")
                sender = quote_item.get("sender", "")
                reason = quote_item.get("reason", "")

                # 计算高度
                content_x = card_x + SummaryImageGenerator.CARD_PADDING
                max_quote_width = card_width - SummaryImageGenerator.CARD_PADDING * 2
                quote_text = f'"{content}"'
                quote_lines = SummaryImageGenerator._wrap_text(quote_text, max_quote_width, font_text)
                reason_lines = SummaryImageGenerator._wrap_text(reason, max_quote_width, font_small)

                quote_line_height = font_text.getbbox('测试')[3] - font_text.getbbox('测试')[1]
                reason_line_height = font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1]

                card_height = 50 + len(quote_lines) * (quote_line_height + 12) + 50 + len(reason_lines) * (reason_line_height + 8) + 40
                card_height = max(card_height, 200)

                # 彩色卡片
                img = SummaryImageGenerator._draw_colorful_card(
                    img,
                    (card_x, y, card_x + card_width, y + card_height),
                    SummaryImageGenerator.BORDER_PINK,
                    radius=25,
                    shadow_strength=15
                )

                # 添加引号装饰（左侧）
                quote_deco_path = os.path.join(plugin_dir, "decorations", "decoration_quote.png")
                img = SummaryImageGenerator._add_decoration_with_glow(
                    img,
                    quote_deco_path,
                    (card_x + 15, y + 15),
                    (35, 35),
                    SummaryImageGenerator.BORDER_PINK
                )

                # 添加角落装饰
                corner_path = os.path.join(plugin_dir, "decorations", "decoration_corner.png")
                img = SummaryImageGenerator._add_corner_decorations(
                    img,
                    (card_x, y, card_x + card_width, y + card_height),
                    corner_path,
                    SummaryImageGenerator.BORDER_PINK
                )

                # 添加心形装饰
                heart_path = os.path.join(plugin_dir, "decorations", "decoration_heart.png")
                img = SummaryImageGenerator._add_decoration_with_glow(
                    img,
                    heart_path,
                    (card_x + card_width - 70, y + 20),
                    (45, 45),
                    SummaryImageGenerator.BORDER_PINK
                )

                # 金句内容
                text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                text_draw = ImageDraw.Draw(text_layer)

                content_y = y + 35
                for line in quote_lines:
                    SummaryImageGenerator._draw_text_with_shadow(
                        text_draw,
                        (content_x, content_y),
                        line,
                        font_text,
                        SummaryImageGenerator.TEXT_COLOR,
                        shadow_offset=2
                    )
                    content_y += quote_line_height + 12

                # 发言人
                sender_y = content_y + 20
                sender_text = f"—— {sender}"
                SummaryImageGenerator._draw_text_with_shadow(
                    text_draw,
                    (content_x, sender_y),
                    sender_text,
                    font_small,
                    SummaryImageGenerator.HIGHLIGHT_COLOR,
                    shadow_offset=2
                )

                # 理由
                reason_y = sender_y + 40
                for line in reason_lines:
                    SummaryImageGenerator._draw_text_with_shadow(
                        text_draw,
                        (content_x, reason_y),
                        line,
                        font_small,
                        SummaryImageGenerator.LIGHT_TEXT_COLOR,
                        shadow_offset=1
                    )
                    reason_y += reason_line_height + 8

                img = Image.alpha_composite(img, text_layer)

                y += card_height + SummaryImageGenerator.CARD_SPACING

        # ===== 炫压抑指数区域 =====
        if depression_index:
            # 添加装饰性分隔线
            img = SummaryImageGenerator._draw_decorative_divider(img, y + 10, width)
            y += 40

            # 标题
            section_title = "炫压抑评级"
            title_bbox = font_section_title.getbbox(section_title)
            section_title_width = title_bbox[2] - title_bbox[0]
            section_title_x = (width - section_title_width) // 2

            # 彩色描边标题
            img = SummaryImageGenerator._draw_colorful_text(
                img,
                (section_title_x, y + 30),
                section_title,
                font_section_title,
                SummaryImageGenerator.TITLE_COLOR,
                outline_color=SummaryImageGenerator.BORDER_PURPLE,
                shadow_radius=8
            )

            # 添加decoration5装饰（炫压抑区域）
            deco5_path = os.path.join(plugin_dir, "decorations", "decoration5.png")
            img = SummaryImageGenerator._add_decoration_with_glow(
                img,
                deco5_path,
                (section_title_x - 150, y + 10),
                (120, 120),
                SummaryImageGenerator.BORDER_PURPLE
            )

            # 右侧镜像
            if os.path.exists(deco5_path):
                try:
                    deco5_img = Image.open(deco5_path).convert("RGBA")
                    w, h = deco5_img.size
                    scale = min(120 / w, 120 / h, 1.0)
                    new_w, new_h = int(w * scale), int(h * scale)
                    if scale < 1.0:
                        deco5_img = deco5_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                    deco5_flipped = deco5_img.transpose(Image.FLIP_LEFT_RIGHT)
                    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    paste_x = section_title_x + section_title_width + 30
                    paste_y = y + 10

                    # 添加柔和光晕
                    glow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    for offset in range(15, 0, -2):
                        alpha = int(40 * (15 - offset) / 15)
                        glow_temp = Image.new('RGBA', (new_w + offset * 2, new_h + offset * 2), (0, 0, 0, 0))
                        glow_temp.paste(deco5_flipped, (offset, offset), deco5_flipped)
                        color_layer = Image.new('RGBA', glow_temp.size, SummaryImageGenerator.BORDER_PURPLE + (alpha,))
                        glow_temp = Image.alpha_composite(glow_temp, color_layer)
                        glow_layer.paste(glow_temp, (paste_x - offset, paste_y - offset), glow_temp)

                    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=8))
                    img = Image.alpha_composite(img, glow_layer)

                    overlay.paste(deco5_flipped, (paste_x, paste_y), deco5_flipped)
                    img = Image.alpha_composite(img, overlay)
                except Exception as e:
                    logger.error(f"添加镜像decoration5失败: {e}")

            y += 150

            # 2列布局,每行2个用户
            depression_list = depression_index[:4]  # 最多4个
            col_width = (card_width - 30) // 2  # 两列,中间间距30

            for row in range(0, len(depression_list), 2):
                row_items = depression_list[row:row+2]

                for col_idx, item in enumerate(row_items):
                    name = item.get("name", "")
                    rank = item.get("rank", "S")
                    comment = item.get("comment", "")
                    user_id = item.get("user_id", "")

                    # 计算该卡片位置
                    col_x = card_x + (col_width + 30) * col_idx
                    card_h = 160  # 增加高度以容纳更长的文言文评价

                    # rank颜色映射
                    rank_colors = {
                        "S": (SummaryImageGenerator.BORDER_MAGENTA, (255, 100, 150)),
                        "A": (SummaryImageGenerator.BORDER_ORANGE, (255, 150, 100)),
                        "B": (SummaryImageGenerator.BORDER_CYAN, (100, 200, 255)),
                        "C": (SummaryImageGenerator.BORDER_GREEN, (120, 220, 150)),
                        "D": (SummaryImageGenerator.BORDER_BLUE, (120, 180, 255)),
                    }
                    border_color, text_color = rank_colors.get(rank, rank_colors["S"])

                    # 绘制卡片
                    img = SummaryImageGenerator._draw_colorful_card(
                        img,
                        (col_x, y, col_x + col_width, y + card_h),
                        border_color,
                        radius=20,
                        shadow_strength=12
                    )

                    # 头像(100px,左侧)
                    avatar_added = False
                    avatar_size = 100
                    avatar_x = col_x + 20  # 左侧边距20
                    avatar_y = y + (card_h - avatar_size) // 2  # 垂直居中

                    if user_id:
                        try:
                            avatar = await SummaryImageGenerator._download_qq_avatar(user_id, size=640)
                            if avatar:
                                circular_avatar = SummaryImageGenerator._create_circular_avatar(avatar, avatar_size)
                                img = SummaryImageGenerator._add_avatar_glow(
                                    img,
                                    circular_avatar,
                                    (avatar_x, avatar_y),
                                    border_color,
                                    glow_radius=8
                                )
                                avatar_added = True
                        except Exception as e:
                            logger.error(f"添加炫压抑指数头像失败 (QQ:{user_id}): {e}")

                    # 文字层 - 右侧显示评级和评价
                    text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    text_draw = ImageDraw.Draw(text_layer)

                    # 右侧区域起始位置
                    right_x = col_x + 20 + avatar_size + 15  # 头像右侧15px间距

                    # Rank大号显示(不要括号)，垂直居中
                    rank_text = rank  # 直接显示S,不要[S]

                    # 计算Rank的尺寸
                    rank_bbox = font_section_title.getbbox(rank_text)
                    rank_width = rank_bbox[2] - rank_bbox[0]
                    rank_height = rank_bbox[3] - rank_bbox[1]

                    # 评价区域宽度
                    max_comment_width = col_width - 20 - avatar_size - 15 - rank_width - 20 - 20

                    # 先计算评价文本的行数和总高度
                    comment_lines = SummaryImageGenerator._wrap_text(comment, max_comment_width, font_small)
                    comment_line_height = font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1]
                    total_comment_height = len(comment_lines) * comment_line_height + (len(comment_lines) - 1) * 5

                    # 计算内容总高度（取rank和comment的最大值）
                    content_height = max(rank_height, total_comment_height)

                    # 垂直居中起始y位置
                    right_y = y + (card_h - content_height) // 2

                    # 绘制Rank - 添加发光特效
                    # 1. 外层光晕（多层渐变）
                    for glow_radius in range(15, 0, -3):
                        glow_alpha = int(80 * (glow_radius / 15))
                        glow_color = text_color + (glow_alpha,)
                        for gx in range(-glow_radius, glow_radius + 1, 2):
                            for gy in range(-glow_radius, glow_radius + 1, 2):
                                if gx*gx + gy*gy <= glow_radius*glow_radius:
                                    text_draw.text(
                                        (right_x + gx, right_y + gy),
                                        rank_text,
                                        font=font_section_title,
                                        fill=glow_color
                                    )

                    # 2. 加粗主体文字
                    for offset_x in [0, 1, 2]:
                        for offset_y in [0, 1, 2]:
                            text_draw.text(
                                (right_x + offset_x, right_y + offset_y),
                                rank_text,
                                font=font_section_title,
                                fill=text_color
                            )

                    # 评价(在rank右侧，垂直对齐到内容中心)
                    comment_x = right_x + rank_width + 20  # Rank右侧20px间距
                    comment_y = right_y + (content_height - total_comment_height) // 2  # 垂直居中对齐

                    # 文本换行绘制
                    for line in comment_lines:
                        SummaryImageGenerator._draw_text_with_shadow(
                            text_draw,
                            (comment_x, comment_y),
                            line,
                            font_small,
                            SummaryImageGenerator.TEXT_COLOR,
                            shadow_offset=1
                        )
                        comment_y += comment_line_height + 5

                    img = Image.alpha_composite(img, text_layer)

                # 每行之后增加y（在内层循环结束后）
                if col_idx == len(row_items) - 1:  # 只在处理完一行后增加
                    y += card_h + 25

            y += 10

        # ===== 用户画像区域=====
        if user_profile:
            # 提取用户画像数据
            user_id = user_profile.get("user_id", "")
            tags = user_profile.get("tags", [])
            active_time = user_profile.get("active_time", "")
            fun_score = user_profile.get("fun_score", 50)
            fun_comment = user_profile.get("fun_comment", "")
            topic_leadership = user_profile.get("topic_leadership", 50)
            topic_comment = user_profile.get("topic_comment", "")
            rank_title = user_profile.get("rank_title", "")
            rank_desc = user_profile.get("rank_desc", "")
            mood = user_profile.get("mood", "中性")
            mood_score = user_profile.get("mood_score", 50)
            mood_reason = user_profile.get("mood_reason", "")

            # 添加装饰性分隔线
            img = SummaryImageGenerator._draw_decorative_divider(img, y + 10, width)
            y += 50  # 减少间距

            # 计算整体卡片高度（与前面计算保持一致）
            indicators_height = 315
            total_card_height = 155 + indicators_height + 20  # 顶部信息155 + 三个指标315 + 底部间距20

            # 绘制整体卡片
            card_x = SummaryImageGenerator.PADDING
            card_width = width - SummaryImageGenerator.PADDING * 2

            img = SummaryImageGenerator._draw_colorful_card(
                img,
                (card_x, y, card_x + card_width, y + total_card_height),
                SummaryImageGenerator.BORDER_CYAN,
                radius=20,
                shadow_strength=12
            )

            # === 顶部区域：左侧头像 + 右侧所有信息 ===
            top_y = y + 20
            avatar_size = 100
            avatar_x = card_x + 25

            # 下载并绘制QQ头像
            if user_id:
                avatar_img = await SummaryImageGenerator._download_qq_avatar(user_id, size=140)
                if avatar_img:
                    avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                    mask = Image.new('L', (avatar_size, avatar_size), 0)
                    mask_draw = ImageDraw.Draw(mask)
                    mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
                    avatar_img.putalpha(mask)

                    glow_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
                    # 增强光晕效果：更多层次，更高透明度，更粗线条
                    for i in range(8, 0, -1):
                        glow_draw = ImageDraw.Draw(glow_layer)
                        glow_draw.ellipse(
                            (avatar_x - i*2, top_y - i*2, avatar_x + avatar_size + i*2, top_y + avatar_size + i*2),
                            outline=SummaryImageGenerator.BORDER_CYAN + (int(255 * (i / 8) * 0.8),),  # 透明度提升到80%
                            width=2
                        )
                    img = Image.alpha_composite(img, glow_layer)
                    img.paste(avatar_img, (avatar_x, top_y), avatar_img)

            # 右侧信息区域
            info_x = avatar_x + avatar_size + 20
            info_y = top_y + 5

            text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            text_draw = ImageDraw.Draw(text_layer)

            # 1. 个性标签（横向排列，支持1-3个）
            current_x = info_x
            if tags:
                # 定义3种渐变色方案
                gradient_colors = [
                    (SummaryImageGenerator.GRADIENT_1_START, SummaryImageGenerator.GRADIENT_1_END),
                    (SummaryImageGenerator.GRADIENT_2_START, SummaryImageGenerator.GRADIENT_2_END),
                    (SummaryImageGenerator.GRADIENT_3_START, SummaryImageGenerator.GRADIENT_3_END),
                ]

                for i, tag_text in enumerate(tags[:3]):  # 支持最多3个标签
                    tag_bbox = font_text.getbbox(tag_text)
                    tag_w = tag_bbox[2] - tag_bbox[0] + 20
                    tag_h = font_text.getbbox('测试')[3] - font_text.getbbox('测试')[1] + 10

                    # 使用对应的渐变色
                    gradient_start, gradient_end = gradient_colors[i % 3]

                    # 使用平均颜色绘制圆角矩形背景
                    r = int((gradient_start[0] + gradient_end[0]) / 2)
                    g = int((gradient_start[1] + gradient_end[1]) / 2)
                    b = int((gradient_start[2] + gradient_end[2]) / 2)

                    text_draw.rounded_rectangle(
                        (current_x, info_y, current_x + tag_w, info_y + tag_h),
                        radius=8,
                        fill=(r, g, b, 200)
                    )

                    # 绘制标签文本
                    text_bbox = font_text.getbbox(tag_text)
                    text_x = current_x + (tag_w - (text_bbox[2] - text_bbox[0])) // 2
                    text_y = info_y + (tag_h - (text_bbox[3] - text_bbox[1])) // 2
                    text_draw.text((text_x, text_y), tag_text, fill=(255, 255, 255, 255), font=font_text)

                    current_x += tag_w + 10

            info_y += 45  # 增加间距，避免标签和活跃时段太紧凑

            # 2. 活跃时段
            active_label = f"活跃时段: {active_time}"
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (info_x, info_y),
                active_label,
                font_text,
                SummaryImageGenerator.SUBTITLE_COLOR,
                shadow_offset=1
            )

            info_y += font_text.getbbox('测试')[3] - font_text.getbbox('测试')[1] + 15

            # 3. 心情指数
            mood_text = f"心情: {mood} {mood_score}分"
            mood_text_y = info_y  # 保存心情文本的y坐标
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (info_x, info_y),
                mood_text,
                font_text,
                SummaryImageGenerator.TITLE_COLOR,
                shadow_offset=1
            )

            mood_text_height = font_text.getbbox('测试')[3] - font_text.getbbox('测试')[1]
            info_y += mood_text_height + 10

            # 心情进度条（简化计算）
            bar_width = 200  # 固定宽度200px，更直观
            bar_height = 8
            bar_x = info_x
            bar_y = info_y

            text_draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + bar_width, bar_y + bar_height),
                radius=4,
                fill=(50, 55, 65, 180)
            )

            filled_width = int(bar_width * mood_score / 100)
            if filled_width > 0:
                if mood_score >= 70:
                    bar_color = (100, 255, 150, 255)
                elif mood_score >= 40:
                    bar_color = (255, 200, 100, 255)
                else:
                    bar_color = (255, 100, 100, 255)

                text_draw.rounded_rectangle(
                    (bar_x, bar_y, bar_x + filled_width, bar_y + bar_height),
                    radius=4,
                    fill=bar_color
                )

            # 心情理由（显示在进度条右侧，垂直居中于心情文本和进度条之间）
            reason_x = bar_x + bar_width + 15  # 进度条右侧15px
            reason_max_width = card_width - (reason_x - card_x) - 30  # 剩余宽度

            # 计算心情文本和进度条的中间位置
            # 心情文本顶部: mood_text_y
            # 进度条底部: bar_y + bar_height
            # 中间位置: (mood_text_y + bar_y + bar_height) / 2
            reason_y = (mood_text_y + bar_y + bar_height) // 2 - (font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1]) // 2

            # 如果剩余宽度足够，显示在右侧；否则换行到下方
            if reason_max_width > 150:  # 至少需要150px宽度
                # 单行显示在右侧，垂直居中
                mood_reason_short = mood_reason[:30] + "..." if len(mood_reason) > 30 else mood_reason
                SummaryImageGenerator._draw_text_with_shadow(
                    text_draw,
                    (reason_x, reason_y),
                    mood_reason_short,
                    font_small,
                    SummaryImageGenerator.LIGHT_TEXT_COLOR,
                    shadow_offset=1
                )
                info_y += 30  # 增加间距从20到30
            else:
                # 换行到下方
                info_y += 25  # 增加间距
                mood_reason_lines = SummaryImageGenerator._wrap_text(mood_reason, card_width - (info_x - card_x) - 40, font_small)
                for line in mood_reason_lines[:1]:  # 最多显示1行
                    SummaryImageGenerator._draw_text_with_shadow(
                        text_draw,
                        (info_x, info_y),
                        line,
                        font_small,
                        SummaryImageGenerator.LIGHT_TEXT_COLOR,
                        shadow_offset=1
                    )
                    info_y += font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1] + 8  # 增加间距

            img = Image.alpha_composite(img, text_layer)

            # === 三个新指标区域 ===
            indicators_y = y + 145 + 30  # 增加间距从20到30

            # 主分隔线
            line_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            line_draw = ImageDraw.Draw(line_layer)
            line_draw.line(
                [(card_x + 30, indicators_y), (card_x + card_width - 30, indicators_y)],
                fill=SummaryImageGenerator.BORDER_CYAN + (100,),
                width=2
            )
            img = Image.alpha_composite(img, line_layer)

            indicators_y += 30

            # 创建文本图层
            text_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            text_draw = ImageDraw.Draw(text_layer)

            # 计算最大宽度
            max_comment_width = card_width - 80

            # === 1. 整活质量评分 ===
            # 标题和分数
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (card_x + 40, indicators_y),
                "整活质量",
                font_text,
                SummaryImageGenerator.TITLE_COLOR,
                shadow_offset=1
            )
            score_text = f"{fun_score}分"
            score_bbox = font_section_title.getbbox(score_text)
            score_width = score_bbox[2] - score_bbox[0]
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (card_x + card_width - score_width - 40, indicators_y - 3),
                score_text,
                font_section_title,
                SummaryImageGenerator.BORDER_CYAN,
                shadow_offset=2
            )
            indicators_y += 32

            # 评价文本
            fun_comment_lines = SummaryImageGenerator._wrap_text(fun_comment, max_comment_width, font_small)
            for idx, line in enumerate(fun_comment_lines[:2]):
                SummaryImageGenerator._draw_text_with_shadow(
                    text_draw,
                    (card_x + 40, indicators_y),
                    line,
                    font_small,
                    SummaryImageGenerator.LIGHT_TEXT_COLOR,
                    shadow_offset=1
                )
                indicators_y += font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1] + 4

            indicators_y += 18

            # 小分隔线
            line_draw = ImageDraw.Draw(line_layer)
            line_draw.line(
                [(card_x + 40, indicators_y), (card_x + card_width - 40, indicators_y)],
                fill=SummaryImageGenerator.BORDER_CYAN + (50,),
                width=1
            )
            img = Image.alpha_composite(img, line_layer)
            indicators_y += 18

            # === 2. 话题引导力 ===
            # 标题和分数
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (card_x + 40, indicators_y),
                "话题引导力",
                font_text,
                SummaryImageGenerator.TITLE_COLOR,
                shadow_offset=1
            )
            leadership_text = f"{topic_leadership}分"
            leadership_bbox = font_section_title.getbbox(leadership_text)
            leadership_width = leadership_bbox[2] - leadership_bbox[0]
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (card_x + card_width - leadership_width - 40, indicators_y - 3),
                leadership_text,
                font_section_title,
                SummaryImageGenerator.GRADIENT_2_START,
                shadow_offset=2
            )
            indicators_y += 32

            # 评价文本
            topic_comment_lines = SummaryImageGenerator._wrap_text(topic_comment, max_comment_width, font_small)
            for idx, line in enumerate(topic_comment_lines[:2]):
                SummaryImageGenerator._draw_text_with_shadow(
                    text_draw,
                    (card_x + 40, indicators_y),
                    line,
                    font_small,
                    SummaryImageGenerator.LIGHT_TEXT_COLOR,
                    shadow_offset=1
                )
                indicators_y += font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1] + 4

            indicators_y += 18

            # 小分隔线
            line_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            line_draw = ImageDraw.Draw(line_layer)
            line_draw.line(
                [(card_x + 40, indicators_y), (card_x + card_width - 40, indicators_y)],
                fill=SummaryImageGenerator.BORDER_CYAN + (50,),
                width=1
            )
            img = Image.alpha_composite(img, line_layer)
            indicators_y += 18

            # === 3. 段位评定 ===
            # 标题和段位名称
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (card_x + 40, indicators_y),
                "今日段位",
                font_text,
                SummaryImageGenerator.TITLE_COLOR,
                shadow_offset=1
            )
            # 段位名称右对齐
            rank_bbox = font_section_title.getbbox(rank_title)
            rank_width = rank_bbox[2] - rank_bbox[0]
            SummaryImageGenerator._draw_text_with_shadow(
                text_draw,
                (card_x + card_width - rank_width - 40, indicators_y - 3),
                rank_title,
                font_section_title,
                SummaryImageGenerator.GRADIENT_3_START,
                shadow_offset=2
            )
            indicators_y += 32

            # 段位描述
            rank_desc_lines = SummaryImageGenerator._wrap_text(rank_desc, max_comment_width, font_small)
            for idx, line in enumerate(rank_desc_lines[:2]):
                SummaryImageGenerator._draw_text_with_shadow(
                    text_draw,
                    (card_x + 40, indicators_y),
                    line,
                    font_small,
                    SummaryImageGenerator.LIGHT_TEXT_COLOR,
                    shadow_offset=1
                )
                indicators_y += font_small.getbbox('测试')[3] - font_small.getbbox('测试')[1] + 4

            img = Image.alpha_composite(img, text_layer)

            y += total_card_height + 25

        # ===== 底部装饰 =====
        y += 30  # 减少底部间距

        # 添加decoration2作为底部大型装饰
        deco2_path = os.path.join(plugin_dir, "decorations", "decoration2.png")
        if os.path.exists(deco2_path):
            try:
                deco2_img = Image.open(deco2_path).convert("RGBA")
                w, h = deco2_img.size
                # 确保完整显示，调整最大尺寸（缩小装饰）
                scale = min(250 / w, 140 / h, 1.0)
                new_w, new_h = int(w * scale), int(h * scale)
                if scale < 1.0:
                    deco2_img = deco2_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                paste_x = (width - new_w) // 2
                paste_y = y + 20

                img = SummaryImageGenerator._add_decoration_with_glow(
                    img,
                    deco2_path,
                    (paste_x, paste_y),
                    (new_w, new_h),
                    SummaryImageGenerator.BORDER_PURPLE
                )
            except Exception as e:
                logger.error(f"添加decoration2失败: {e}")

        # 添加气泡装饰（调整位置）
        bubble_path = os.path.join(plugin_dir, "decorations", "decoration_bubble.png")
        bubble_positions = [
            (120, y + 15),
            (width - 170, y + 20),
            (180, y + 70),
            (width - 230, y + 75),
        ]
        for pos in bubble_positions:
            img = SummaryImageGenerator._add_decoration_with_glow(
                img,
                bubble_path,
                pos,
                (60, 60),
                SummaryImageGenerator.BORDER_BLUE
            )

        # 转换为RGB并保存到项目 images 目录
        img = img.convert('RGB')

        # 创建临时文件，保存到项目的 data/images 目录
        try:
            # 获取项目根目录
            current_dir = os.path.dirname(os.path.abspath(__file__))
            # 向上三级到达 MaiBot 目录
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
            images_dir = os.path.join(project_root, "data", "images")

            # 确保目录存在
            os.makedirs(images_dir, exist_ok=True)

            # 生成唯一文件名
            import uuid
            filename = f"summary_{uuid.uuid4().hex[:8]}.jpg"
            img_path = os.path.join(images_dir, filename)

            # 保存图片
            img.save(img_path, format='JPEG', quality=90, optimize=True)

            if not os.path.exists(img_path):
                raise IOError(f"图片保存失败")

            return img_path
        except Exception as e:
            logger.error(f"保存图片失败: {e}", exc_info=True)
            raise
