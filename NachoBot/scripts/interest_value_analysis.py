import time
import sys
import os
from typing import Dict, List, Tuple, Optional
from datetime import datetime

# Add project root to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
from src.common.database.database_model import Messages, ChatStreams  # noqa


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


def calculate_interest_value_distribution(messages) -> Dict[str, int]:
    """Calculate distribution of interest_value"""
    distribution = {
        "0.000-0.010": 0,
        "0.010-0.050": 0,
        "0.050-0.100": 0,
        "0.100-0.500": 0,
        "0.500-1.000": 0,
        "1.000-2.000": 0,
        "2.000-5.000": 0,
        "5.000-10.000": 0,
        "10.000+": 0,
    }

    for msg in messages:
        if msg.interest_value is None or msg.interest_value == 0.0:
            continue

        value = float(msg.interest_value)
        if value < 0.010:
            distribution["0.000-0.010"] += 1
        elif value < 0.050:
            distribution["0.010-0.050"] += 1
        elif value < 0.100:
            distribution["0.050-0.100"] += 1
        elif value < 0.500:
            distribution["0.100-0.500"] += 1
        elif value < 1.000:
            distribution["0.500-1.000"] += 1
        elif value < 2.000:
            distribution["1.000-2.000"] += 1
        elif value < 5.000:
            distribution["2.000-5.000"] += 1
        elif value < 10.000:
            distribution["5.000-10.000"] += 1
        else:
            distribution["10.000+"] += 1

    return distribution


def get_interest_value_stats(messages) -> Dict[str, float]:
    """Calculate basic statistics for interest_value"""
    values = [
        float(msg.interest_value) for msg in messages if msg.interest_value is not None and msg.interest_value != 0.0
    ]

    if not values:
        return {"count": 0, "min": 0, "max": 0, "avg": 0, "median": 0}

    values.sort()
    count = len(values)

    return {
        "count": count,
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / count,
        "median": values[count // 2] if count % 2 == 1 else (values[count // 2 - 1] + values[count // 2]) / 2,
    }


def get_available_chats() -> List[Tuple[str, str, int]]:
    """Get all available chats with message counts"""
    try:
        # 获取所有有消息的chat_id
        chat_counts = {}
        for msg in Messages.select(Messages.chat_id).distinct():
            chat_id = msg.chat_id
            count = (
                Messages.select()
                .where(
                    (Messages.chat_id == chat_id)
                    & (Messages.interest_value.is_null(False))
                    & (Messages.interest_value != 0.0)
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


def analyze_interest_values(
    chat_id: Optional[str] = None, start_time: Optional[float] = None, end_time: Optional[float] = None
) -> None:
    """Analyze interest values with optional filters"""

    # 构建查询条件
    query = Messages.select().where((Messages.interest_value.is_null(False)) & (Messages.interest_value != 0.0))

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
    distribution = calculate_interest_value_distribution(messages)
    stats = get_interest_value_stats(messages)

    # 显示结果
    print("\n=== Interest Value 分析结果 ===")
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
    print(f"有效消息数量: {stats['count']} (排除null和0值)")
    print(f"最小值: {stats['min']:.3f}")
    print(f"最大值: {stats['max']:.3f}")
    print(f"平均值: {stats['avg']:.3f}")
    print(f"中位数: {stats['median']:.3f}")

    print("\nInterest Value 分布:")
    total = stats["count"]
    for range_name, count in distribution.items():
        if count > 0:
            percentage = count / total * 100
            print(f"{range_name}: {count} ({percentage:.2f}%)")


def interactive_menu() -> None:
    """Interactive menu for interest value analysis"""

    while True:
        print("\n" + "=" * 50)
        print("Interest Value 分析工具")
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
                print("没有找到有interest_value数据的聊天")
                continue

            print(f"\n可用的聊天 (共{len(chats)}个):")
            for i, (_cid, name, count) in enumerate(chats, 1):
                print(f"{i}. {name} ({count}条有效消息)")

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
        analyze_interest_values(chat_id, start_time, end_time)

        input("\n按回车键继续...")


if __name__ == "__main__":
    interactive_menu()
