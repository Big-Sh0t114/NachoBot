import asyncio
import os
import sys

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.common.database.database_model import GroupInfo, initialize_database
from src.common.database.database import db


async def main():
    initialize_database()

    # Get all Discord groups
    groups = GroupInfo.select().where(GroupInfo.platform == "discord")

    print(f"{'Group ID':<20} | {'Group Name':<20} | {'Topic'}")
    print("-" * 60)

    if not groups:
        print("No Discord groups found in GroupInfo.")
        return

    for group in groups:
        print(f"{str(group.group_id):<20} | {str(group.group_name):<20} | {str(group.topic)}")


if __name__ == "__main__":
    asyncio.run(main())
