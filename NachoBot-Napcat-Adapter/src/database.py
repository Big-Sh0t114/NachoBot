import os
from typing import Optional, List
from dataclasses import dataclass
from sqlmodel import Field, Session, SQLModel, create_engine, select

from src.logger import logger

"""
表记录的方式：
| group_id | user_id | lift_time |
|----------|---------|-----------|

其中使用 user_id == 0 表示群全体禁言
"""


@dataclass
class BanUser:
    """
    程序处理使用的实例
    """

    user_id: int
    group_id: int
    lift_time: Optional[int] = Field(default=-1)


class DB_BanUser(SQLModel, table=True):
    """
    表示数据库中的用户禁言记录。
    使用双重主键
    """

    user_id: int = Field(index=True, primary_key=True)  # 被禁言用户的用户 ID
    group_id: int = Field(index=True, primary_key=True)  # 用户被禁言的群组 ID
    lift_time: Optional[int]  # 禁言解除的时间（时间戳）


def is_identical(obj1: BanUser, obj2: BanUser) -> bool:
    """
    检查两个 BanUser 对象是否相同。
    """
    return obj1.user_id == obj2.user_id and obj1.group_id == obj2.group_id


class DatabaseManager:
    """
    数据库管理类，负责与数据库交互。
    """

    def __init__(self):
        os.makedirs(os.path.join(os.path.dirname(__file__), "..", "data"), exist_ok=True)  # 确保数据目录存在
        DATABASE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "NapcatAdapter.db")
        self.sqlite_url = f"sqlite:///{DATABASE_FILE}"  # SQLite 数据库 URL
        self.engine = create_engine(self.sqlite_url, echo=False)  # 创建数据库引擎
        self._ensure_database()  # 确保数据库和表已创建

    def _ensure_database(self) -> None:
        """
        确保数据库和表已创建。
        """
        logger.info("确保数据库文件和表已创建...")
        SQLModel.metadata.create_all(self.engine)
        logger.success("数据库和表已创建或已存在")

    def update_ban_record(self, ban_list: List[BanUser]) -> None:
        # sourcery skip: class-extract-method
        """
        更新禁言列表到数据库。
        支持在不存在时创建新记录，对于多余的项目自动删除。
        """
        with Session(self.engine) as session:
            all_records = session.exec(select(DB_BanUser)).all()
            for ban_user in ban_list:
                statement = select(DB_BanUser).where(
                    DB_BanUser.user_id == ban_user.user_id, DB_BanUser.group_id == ban_user.group_id
                )
                if existing_record := session.exec(statement).first():
                    if existing_record.lift_time == ban_user.lift_time:
                        logger.debug(f"禁言记录未变更: {existing_record}")
                        continue
                    # 更新现有记录的 lift_time
                    existing_record.lift_time = ban_user.lift_time
                    session.add(existing_record)
                    logger.debug(f"更新禁言记录: {existing_record}")
                else:
                    # 创建新记录
                    db_record = DB_BanUser(
                        user_id=ban_user.user_id, group_id=ban_user.group_id, lift_time=ban_user.lift_time
                    )
                    session.add(db_record)
                    logger.debug(f"创建新禁言记录: {ban_user}")
            # 删除不在 ban_list 中的记录
            for db_record in all_records:
                record = BanUser(user_id=db_record.user_id, group_id=db_record.group_id, lift_time=db_record.lift_time)
                if not any(is_identical(record, ban_user) for ban_user in ban_list):
                    statement = select(DB_BanUser).where(
                        DB_BanUser.user_id == record.user_id, DB_BanUser.group_id == record.group_id
                    )
                    if ban_record := session.exec(statement).first():
                        session.delete(ban_record)
                        session.commit()
                        logger.debug(f"删除禁言记录: {ban_record}")
                    else:
                        logger.info(f"未找到禁言记录: {ban_record}")

            session.commit()
            logger.info("禁言记录已更新")

    def get_ban_records(self) -> List[BanUser]:
        """
        读取所有禁言记录。
        """
        with Session(self.engine) as session:
            statement = select(DB_BanUser)
            records = session.exec(statement).all()
            return [BanUser(user_id=item.user_id, group_id=item.group_id, lift_time=item.lift_time) for item in records]

    def create_ban_record(self, ban_record: BanUser) -> None:
        """
        为特定群组中的用户创建禁言记录。
        一个简化版本的添加方式，防止 update_ban_record 方法的复杂性。
        其同时还是简化版的更新方式。
        """
        with Session(self.engine) as session:
            # 检查记录是否已存在
            statement = select(DB_BanUser).where(
                DB_BanUser.user_id == ban_record.user_id, DB_BanUser.group_id == ban_record.group_id
            )
            existing_record = session.exec(statement).first()
            if existing_record:
                # 如果记录已存在，更新 lift_time
                existing_record.lift_time = ban_record.lift_time
                session.add(existing_record)
                logger.debug(f"更新禁言记录: {ban_record}")
            else:
                # 如果记录不存在，创建新记录
                db_record = DB_BanUser(
                    user_id=ban_record.user_id, group_id=ban_record.group_id, lift_time=ban_record.lift_time
                )
                session.add(db_record)
                logger.debug(f"创建新禁言记录: {ban_record}")
            session.commit()

    def delete_ban_record(self, ban_record: BanUser):
        """
        删除特定用户在特定群组中的禁言记录。
        一个简化版本的删除方式，防止 update_ban_record 方法的复杂性。
        """
        user_id = ban_record.user_id
        group_id = ban_record.group_id
        with Session(self.engine) as session:
            statement = select(DB_BanUser).where(DB_BanUser.user_id == user_id, DB_BanUser.group_id == group_id)
            if ban_record := session.exec(statement).first():
                session.delete(ban_record)
                session.commit()
                logger.debug(f"删除禁言记录: {ban_record}")
            else:
                logger.info(f"未找到禁言记录: user_id: {user_id}, group_id: {group_id}")


db_manager = DatabaseManager()
