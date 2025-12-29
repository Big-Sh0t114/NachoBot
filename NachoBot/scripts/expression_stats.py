import time
import sys
import os
from typing import Dict, List

# Add project root to Python path
from src.common.database.database_model import Expression, ChatStreams

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)


def get_chat_name(chat_id: str) -> str:
    """Get chat name from chat_id by querying ChatStreams table directly"""
    try:
        # 直接从数据库查询ChatStreams表
        chat_stream = ChatStreams.get_or_none(ChatStreams.stream_id == chat_id)
        if chat_stream is None:
            return f"未知聊天 ({chat_id})"

        # 如果有群组信息，显示群组名称
        if chat_stream.group_name:
            return f"{chat_stream.group_name} ({chat_id})"
        # 如果是私聊，显示用户昵称
        elif chat_stream.user_nickname:
            return f"{chat_stream.user_nickname}的私聊 ({chat_id})"
        else:
            return f"未知聊天 ({chat_id})"
    except Exception:
        return f"查询失败 ({chat_id})"


def calculate_time_distribution(expressions) -> Dict[str, int]:
    """Calculate distribution of last active time in days"""
    now = time.time()
    distribution = {
        "0-1天": 0,
        "1-3天": 0,
        "3-7天": 0,
        "7-14天": 0,
        "14-30天": 0,
        "30-60天": 0,
        "60-90天": 0,
        "90+天": 0,
    }
    for expr in expressions:
        diff_days = (now - expr.last_active_time) / (24 * 3600)
        if diff_days < 1:
            distribution["0-1天"] += 1
        elif diff_days < 3:
            distribution["1-3天"] += 1
        elif diff_days < 7:
            distribution["3-7天"] += 1
        elif diff_days < 14:
            distribution["7-14天"] += 1
        elif diff_days < 30:
            distribution["14-30天"] += 1
        elif diff_days < 60:
            distribution["30-60天"] += 1
        elif diff_days < 90:
            distribution["60-90天"] += 1
        else:
            distribution["90+天"] += 1
    return distribution


def calculate_count_distribution(expressions) -> Dict[str, int]:
    """Calculate distribution of count values"""
    distribution = {"0-1": 0, "1-2": 0, "2-3": 0, "3-4": 0, "4-5": 0, "5-10": 0, "10+": 0}
    for expr in expressions:
        cnt = expr.count
        if cnt < 1:
            distribution["0-1"] += 1
        elif cnt < 2:
            distribution["1-2"] += 1
        elif cnt < 3:
            distribution["2-3"] += 1
        elif cnt < 4:
            distribution["3-4"] += 1
        elif cnt < 5:
            distribution["4-5"] += 1
        elif cnt < 10:
            distribution["5-10"] += 1
        else:
            distribution["10+"] += 1
    return distribution


def get_top_expressions_by_chat(chat_id: str, top_n: int = 5) -> List[Expression]:
    """Get top N most used expressions for a specific chat_id"""
    return Expression.select().where(Expression.chat_id == chat_id).order_by(Expression.count.desc()).limit(top_n)


def show_overall_statistics(expressions, total: int) -> None:
    """Show overall statistics"""
    time_dist = calculate_time_distribution(expressions)
    count_dist = calculate_count_distribution(expressions)

    print("\n=== 总体统计 ===")
    print(f"总表达式数量: {total}")

    print("\n上次激活时间分布:")
    for period, count in time_dist.items():
        print(f"{period}: {count} ({count / total * 100:.2f}%)")

    print("\ncount分布:")
    for range_, count in count_dist.items():
        print(f"{range_}: {count} ({count / total * 100:.2f}%)")


def show_chat_statistics(chat_id: str, chat_name: str) -> None:
    """Show statistics for a specific chat"""
    chat_exprs = list(Expression.select().where(Expression.chat_id == chat_id))
    chat_total = len(chat_exprs)

    print(f"\n=== {chat_name} ===")
    print(f"表达式数量: {chat_total}")

    if chat_total == 0:
        print("该聊天没有表达式数据")
        return

    # Time distribution for this chat
    time_dist = calculate_time_distribution(chat_exprs)
    print("\n上次激活时间分布:")
    for period, count in time_dist.items():
        if count > 0:
            print(f"{period}: {count} ({count / chat_total * 100:.2f}%)")

    # Count distribution for this chat
    count_dist = calculate_count_distribution(chat_exprs)
    print("\ncount分布:")
    for range_, count in count_dist.items():
        if count > 0:
            print(f"{range_}: {count} ({count / chat_total * 100:.2f}%)")

    # Top expressions
    print("\nTop 10使用最多的表达式:")
    top_exprs = get_top_expressions_by_chat(chat_id, 10)
    for i, expr in enumerate(top_exprs, 1):
        print(f"{i}. [{expr.type}] Count: {expr.count}")
        print(f"   Situation: {expr.situation}")
        print(f"   Style: {expr.style}")
        print()


def interactive_menu() -> None:
    """Interactive menu for expression statistics"""
    # Get all expressions
    expressions = list(Expression.select())
    if not expressions:
        print("数据库中没有找到表达式")
        return

    total = len(expressions)

    # Get unique chat_ids and their names
    chat_ids = list(set(expr.chat_id for expr in expressions))
    chat_info = [(chat_id, get_chat_name(chat_id)) for chat_id in chat_ids]
    chat_info.sort(key=lambda x: x[1])  # Sort by chat name

    while True:
        print("\n" + "=" * 50)
        print("表达式统计分析")
        print("=" * 50)
        print("0. 显示总体统计")

        for i, (chat_id, chat_name) in enumerate(chat_info, 1):
            chat_count = sum(1 for expr in expressions if expr.chat_id == chat_id)
            print(f"{i}. {chat_name} ({chat_count}个表达式)")

        print("q. 退出")

        choice = input("\n请选择要查看的统计 (输入序号): ").strip()

        if choice.lower() == "q":
            print("再见！")
            break

        try:
            choice_num = int(choice)
            if choice_num == 0:
                show_overall_statistics(expressions, total)
            elif 1 <= choice_num <= len(chat_info):
                chat_id, chat_name = chat_info[choice_num - 1]
                show_chat_statistics(chat_id, chat_name)
            else:
                print("无效的选择，请重新输入")
        except ValueError:
            print("请输入有效的数字")

        input("\n按回车键继续...")


if __name__ == "__main__":
    interactive_menu()
