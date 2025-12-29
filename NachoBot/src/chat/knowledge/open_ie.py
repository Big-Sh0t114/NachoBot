import json
import os
import glob
from typing import Any, Dict, List


from . import INVALID_ENTITY, ROOT_PATH, DATA_PATH
# from src.manager.local_store_manager import local_storage


def _filter_invalid_entities(entities: List[str]) -> List[str]:
    """过滤无效的实体"""
    valid_entities = set()
    for entity in entities:
        if not isinstance(entity, str) or entity.strip() == "" or entity in INVALID_ENTITY or entity in valid_entities:
            # 非字符串/空字符串/在无效实体列表中/重复
            continue
        valid_entities.add(entity)

    return list(valid_entities)


def _filter_invalid_triples(triples: List[List[str]]) -> List[List[str]]:
    """过滤无效的三元组"""
    unique_triples = set()
    valid_triples = []

    for triple in triples:
        if len(triple) != 3 or (
            (not isinstance(triple[0], str) or triple[0].strip() == "")
            or (not isinstance(triple[1], str) or triple[1].strip() == "")
            or (not isinstance(triple[2], str) or triple[2].strip() == "")
        ):
            # 三元组长度不为3，或其中存在空值
            continue

        valid_triple = [str(item) for item in triple]
        if tuple(valid_triple) not in unique_triples:
            unique_triples.add(tuple(valid_triple))
            valid_triples.append(valid_triple)

    return valid_triples


class OpenIE:
    """
    OpenIE规约的数据格式为如下
    {
        "docs": [
            {
                "idx": "文档的唯一标识符（通常是文本的SHA256哈希值）",
                "passage": "文档的原始文本",
                "extracted_entities": ["实体1", "实体2", ...],
                "extracted_triples": [["主语", "谓语", "宾语"], ...]
            },
            ...
        ],
        "avg_ent_chars": "实体平均字符数",
        "avg_ent_words": "实体平均词数"
    }
    """

    def __init__(
        self,
        docs: List[Dict[str, Any]],
        avg_ent_chars,
        avg_ent_words,
    ):
        self.docs = docs
        self.avg_ent_chars = avg_ent_chars
        self.avg_ent_words = avg_ent_words

        for doc in self.docs:
            # 过滤实体列表
            doc["extracted_entities"] = _filter_invalid_entities(doc["extracted_entities"])
            # 过滤无效的三元组
            doc["extracted_triples"] = _filter_invalid_triples(doc["extracted_triples"])

    @staticmethod
    def _from_dict(data_list):
        """从多个字典合并OpenIE对象"""
        # data_list: List[dict]
        all_docs = []
        for data in data_list:
            all_docs.extend(data.get("docs", []))
        # 重新计算统计
        sum_phrase_chars = sum([len(e) for chunk in all_docs for e in chunk["extracted_entities"]])
        sum_phrase_words = sum([len(e.split()) for chunk in all_docs for e in chunk["extracted_entities"]])
        num_phrases = sum([len(chunk["extracted_entities"]) for chunk in all_docs])
        avg_ent_chars = round(sum_phrase_chars / num_phrases, 4) if num_phrases else 0
        avg_ent_words = round(sum_phrase_words / num_phrases, 4) if num_phrases else 0
        return OpenIE(
            docs=all_docs,
            avg_ent_chars=avg_ent_chars,
            avg_ent_words=avg_ent_words,
        )

    def _to_dict(self):
        """转换为字典"""
        return {
            "docs": self.docs,
            "avg_ent_chars": self.avg_ent_chars,
            "avg_ent_words": self.avg_ent_words,
        }

    @staticmethod
    def load() -> "OpenIE":
        """从OPENIE_DIR下所有json文件合并加载OpenIE数据"""
        openie_dir = os.path.join(DATA_PATH, "openie")
        if not os.path.exists(openie_dir):
            raise Exception(f"OpenIE数据目录不存在: {openie_dir}")
        json_files = sorted(glob.glob(os.path.join(openie_dir, "*.json")))
        data_list = []
        for file in json_files:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
                data_list.append(data)
        if not data_list:
            # print(f"111111111111111111111Root Path : \n{ROOT_PATH}")
            raise Exception(f"未在 {openie_dir} 找到任何OpenIE json文件")
        openie_data = OpenIE._from_dict(data_list)
        return openie_data

    def extract_entity_dict(self):
        """提取实体列表"""
        ner_output_dict = dict(
            {
                doc_item["idx"]: doc_item["extracted_entities"]
                for doc_item in self.docs
                if len(doc_item["extracted_entities"]) > 0
            }
        )
        return ner_output_dict

    def extract_triple_dict(self):
        """提取三元组列表"""
        triple_output_dict = dict(
            {
                doc_item["idx"]: doc_item["extracted_triples"]
                for doc_item in self.docs
                if len(doc_item["extracted_triples"]) > 0
            }
        )
        return triple_output_dict

    def extract_raw_paragraph_dict(self):
        """提取原始段落"""
        raw_paragraph_dict = dict({doc_item["idx"]: doc_item["passage"] for doc_item in self.docs})
        return raw_paragraph_dict


if __name__ == "__main__":
    # 测试代码
    print(ROOT_PATH)
