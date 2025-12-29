import time
import sys
import os
import re
from typing import Dict, List, Tuple, Optional
from datetime import datetime

# Add project root to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
from src.common.database.database_model import Messages, ChatStreams  # noqa


def contains_emoji_or_image_tags(text: str) -> bool:
    """Check if text contains [表情包xxxxx] or [图片xxxxx] tags"""
    if not text:
        return False

    # 检查是否包含 [表情包] 或 [图片] 标记
    emoji_pattern = r"\[表情包[^\]]*\]"
    image_pattern = r"\[图片[^\]]*\]"

    return bool(re.search(emoji_pattern, text) or re.search(image_pattern, text))


def clean_reply_text(text: str) -> str:
    """Remove reply references like [回复 xxxx...] from text"""
    if not text:
        return text

    # 匹配 [回复 xxxx...] 格式的内容
    # 使用非贪婪匹配，匹配到第一个 ] 就停止
    cleaned_text = re.sub(r"\[回复[^\]]*\]", "", text)

    # 去除多余的空白字符
    cleaned_text = cleaned_text.strip()

    return cleaned_text


def get_chat_name(chat_id: str) -> str:
    """Get chat name from chat_id by querying ChatStreams table directly"""
    try:
        chat_stream = ChatStreams.get_or_none(ChatStreams.stream_id == chat_id)
        if chat_stream is None:
            return f"未知聊天 ({chat_id})"

        if chat_stream.group_name:
            return f"{chat_stream.group_name} ({chat_id})"
        elif chat_stream.user_nickname:
            return f"{chat_stream.user_nickname}的私聊 ({chat_id})"
        else:
            return f"未知聊天 ({chat_id})"
    except Exception:
        return f"查询失败 ({chat_id})"


def format_timestamp(timestamp: float) -> str:
    """Format timestamp to readable date string"""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return "未知时间"


def calculate_text_length_distribution(messages) -> Dict[str, int]:
    """Calculate distribution of processed_plain_text length"""
    distribution = {
        "0": 0,  # 空文本
        "1-5": 0,  # 极短文本
        "6-10": 0,  # 很短文本
        "11-20": 0,  # 短文本
        "21-30": 0,  # 较短文本
        "31-50": 0,  # 中短文本
        "51-70": 0,  # 中等文本
        "71-100": 0,  # 较长文本
        "101-150": 0,  # 长文本
        "151-200": 0,  # 很长文本
        "201-300": 0,  # 超长文本
        "301-500": 0,  # 极长文本
        "501-1000": 0,  # 巨长文本
        "1000+": 0,  # 超巨长文本
    }

    for msg in messages:
        if msg.processed_plain_text is None:
            continue

        # 排除包含表情包或图片标记的消息
        if contains_emoji_or_image_tags(msg.processed_plain_text):
            continue

        # 清理文本中的回复引用
        cleaned_text = clean_reply_text(msg.processed_plain_text)
        length = len(cleaned_text)

        if length == 0:
            distribution["0"] += 1
        elif length <= 5:
            distribution["1-5"] += 1
        elif length <= 10:
            distribution["6-10"] += 1
        elif length <= 20:
            distribution["11-20"] += 1
        elif length <= 30:
            distribution["21-30"] += 1
        elif length <= 50:
            distribution["31-50"] += 1
        elif length <= 70:
            distribution["51-70"] += 1
        elif length <= 100:
            distribution["71-100"] += 1
        elif length <= 150:
            distribution["101-150"] += 1
        elif length <= 200:
            distribution["151-200"] += 1
        elif length <= 300:
            distribution["201-300"] += 1
        elif length <= 500:
            distribution["301-500"] += 1
        elif length <= 1000:
            distribution["501-1000"] += 1
        else:
            distribution["1000+"] += 1

    return distribution


def get_text_length_stats(messages) -> Dict[str, float]:
    """Calculate basic statistics for processed_plain_text length"""
    lengths = []
    null_count = 0
    excluded_count = 0  # 被排除的消息数量

    for msg in messages:
        if msg.processed_plain_text is None:
            null_count += 1
        elif contains_emoji_or_image_tags(msg.processed_plain_text):
            # 排除包含表情包或图片标记的消息
            excluded_count += 1
        else:
            # 清理文本中的回复引用
            cleaned_text = clean_reply_text(msg.processed_plain_text)
            lengths.append(len(cleaned_text))

    if not lengths:
        return {
            "count": 0,
            "null_count": null_count,
            "excluded_count": excluded_count,
            "min": 0,
            "max": 0,
            "avg": 0,
            "median": 0,
        }

    lengths.sort()
    count = len(lengths)

    return {
        "count": count,
        "null_count": null_count,
        "excluded_count": excluded_count,
        "min": min(lengths),
        "max": max(lengths),
        "avg": sum(lengths) / count,
        "median": lengths[count // 2] if count % 2 == 1 else (lengths[count // 2 - 1] + lengths[count // 2]) / 2,
    }


def get_available_chats() -> List[Tuple[str, str, int]]:
    """Get all available chats with message counts"""
    try:
        # 获取所有有消息的chat_id，排除特殊类型消息
        chat_counts = {}
        for msg in Messages.select(Messages.chat_id).distinct():
            chat_id = msg.chat_id
            count = (
                Messages.select()
                .where(
                    (Messages.chat_id == chat_id)
                    & (Messages.is_emoji != 1)
                    & (Messages.is_picid != 1)
                    & (Messages.is_command != 1)
                )
                .count()
            )
            if count > 0:
                chat_counts[chat_id] = count

        # 获取聊天名称
        result = []
        for chat_id, count in chat_counts.items():
            chat_name = get_chat_name(chat_id)
            result.append((chat_id, chat_name, count))

        # 按消息数量排序
        result.sort(key=lambda x: x[2], reverse=True)
        return result
    except Exception as e:
        print(f"获取聊天列表失败: {e}")
        return []


def get_time_range_input() -> Tuple[Optional[float], Optional[float]]:
    """Get time range input from user"""
    print("\n时间范围选择:")
    print("1. 最近1天")
    print("2. 最近3天")
    print("3. 最近7天")
    print("4. 最近30天")
    print("5. 自定义时间范围")
    print("6. 不限制时间")

    choice = input("请选择时间范围 (1-6): ").strip()

    now = time.time()

    if choice == "1":
        return now - 24 * 3600, now
    elif choice == "2":
        return now - 3 * 24 * 3600, now
    elif choice == "3":
        return now - 7 * 24 * 3600, now
    elif choice == "4":
        return now - 30 * 24 * 3600, now
    elif choice == "5":
        print("请输入开始时间 (格式: YYYY-MM-DD HH:MM:SS):")
        start_str = input().strip()
        print("请输入结束时间 (格式: YYYY-MM-DD HH:MM:SS):")
        end_str = input().strip()

        try:
            start_time = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").timestamp()
            end_time = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S").timestamp()
            return start_time, end_time
        except ValueError:
            print("时间格式错误，将不限制时间范围")
            return None, None
    else:
        return None, None


def get_top_longest_messages(messages, top_n: int = 10) -> List[Tuple[str, int, str, str]]:
    """Get top N longest messages"""
    message_lengths = []

    for msg in messages:
        if msg.processed_plain_text is not None:
            # 排除包含表情包或图片标记的消息
            if contains_emoji_or_image_tags(msg.processed_plain_text):
                continue

            # 清理文本中的回复引用
            cleaned_text = clean_reply_text(msg.processed_plain_text)
            length = len(cleaned_text)
            chat_name = get_chat_name(msg.chat_id)
            time_str = format_timestamp(msg.time)
            # 截取前100个字符作为预览
            preview = cleaned_text[:100] + "..." if len(cleaned_text) > 100 else cleaned_text
            message_lengths.append((chat_name, length, time_str, preview))

    # 按长度排序，取前N个
    message_lengths.sort(key=lambda x: x[1], reverse=True)
    return message_lengths[:top_n]


def analyze_text_lengths(
    chat_id: Optional[str] = None, start_time: Optional[float] = None, end_time: Optional[float] = None
) -> None:
    """Analyze processed_plain_text lengths with optional filters"""

    # 构建查询条件，排除特殊类型的消息
    query = Messages.select().where((Messages.is_emoji != 1) & (Messages.is_picid != 1) & (Messages.is_command != 1))

    if chat_id:
        query = query.where(Messages.chat_id == chat_id)

    if start_time:
        query = query.where(Messages.time >= start_time)

    if end_time:
        query = query.where(Messages.time <= end_time)

    messages = list(query)

    if not messages:
        print("没有找到符合条件的消息")
        return

    # 计算统计信息
    distribution = calculate_text_length_distribution(messages)
    stats = get_text_length_stats(messages)
    top_longest = get_top_longest_messages(messages, 10)

    # 显示结果
    print("\n=== Processed Plain Text 长度分析结果 ===")
    print("(已排除表情、图片ID、命令类型消息，已排除[表情包]和[图片]标记消息，已清理回复引用)")
    if chat_id:
        print(f"聊天: {get_chat_name(chat_id)}")
    else:
        print("聊天: 全部聊天")

    if start_time and end_time:
        print(f"时间范围: {format_timestamp(start_time)} 到 {format_timestamp(end_time)}")
    elif start_time:
        print(f"时间范围: {format_timestamp(start_time)} 之后")
    elif end_time:
        print(f"时间范围: {format_timestamp(end_time)} 之前")
    else:
        print("时间范围: 不限制")

    print("\n基本统计:")
    print(f"总消息数量: {len(messages)}")
    print(f"有文本消息数量: {stats['count']}")
    print(f"空文本消息数量: {stats['null_count']}")
    print(f"被排除的消息数量: {stats['excluded_count']}")
    if stats["count"] > 0:
        print(f"最短长度: {stats['min']} 字符")
        print(f"最长长度: {stats['max']} 字符")
        print(f"平均长度: {stats['avg']:.2f} 字符")
        print(f"中位数长度: {stats['median']:.2f} 字符")

    print("\n文本长度分布:")
    total = stats["count"]
    if total > 0:
        for range_name, count in distribution.items():
            if count > 0:
                percentage = count / total * 100
                print(f"{range_name} 字符: {count} ({percentage:.2f}%)")

    # 显示最长的消息
    if top_longest:
        print(f"\n最长的 {len(top_longest)} 条消息:")
        for i, (chat_name, length, time_str, preview) in enumerate(top_longest, 1):
            print(f"{i}. [{chat_name}] {time_str}")
            print(f"   长度: {length} 字符")
            print(f"   预览: {preview}")
            print()


def interactive_menu() -> None:
    """Interactive menu for text length analysis"""

    while True:
        print("\n" + "=" * 50)
        print("Processed Plain Text 长度分析工具")
        print("=" * 50)
        print("1. 分析全部聊天")
        print("2. 选择特定聊天分析")
        print("q. 退出")

        choice = input("\n请选择分析模式 (1-2, q): ").strip()

        if choice.lower() == "q":
            print("再见！")
            break

        chat_id = None

        if choice == "2":
            # 显示可用的聊天列表
            chats = get_available_chats()
            if not chats:
                print("没有找到聊天数据")
                continue

            print(f"\n可用的聊天 (共{len(chats)}个):")
            for i, (_cid, name, count) in enumerate(chats, 1):
                print(f"{i}. {name} ({count}条消息)")

            try:
                chat_choice = int(input(f"\n请选择聊天 (1-{len(chats)}): ").strip())
                if 1 <= chat_choice <= len(chats):
                    chat_id = chats[chat_choice - 1][0]
                else:
                    print("无效选择")
                    continue
            except ValueError:
                print("请输入有效数字")
                continue

        elif choice != "1":
            print("无效选择")
            continue

        # 获取时间范围
        start_time, end_time = get_time_range_input()

        # 执行分析
        analyze_text_lengths(chat_id, start_time, end_time)

        input("\n按回车键继续...")


if __name__ == "__main__":
    interactive_menu()
