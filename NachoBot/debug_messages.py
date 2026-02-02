import asyncio
import os
import sys
from pprint import pformat

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.common.database.database_model import Messages, initialize_database
from src.common.database.database import db


async def main():
    initialize_database()

    # Get last 1 non-qq messages
    messages = Messages.select().where(Messages.chat_info_platform != "qq").order_by(Messages.time.desc()).limit(1)

    with open("message_dump.txt", "w", encoding="utf-8") as f:
        if not messages:
            f.write("No non-qq messages found.")
            return

        for msg in messages:
            f.write("Message Data:\n")
            data = {}
            for key in msg.__data__:
                data[key] = msg.__data__[key]
            f.write(pformat(data))


if __name__ == "__main__":
    asyncio.run(main())
