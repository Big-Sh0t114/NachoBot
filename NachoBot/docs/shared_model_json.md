# 模型输出中的单对象 JSON 解析

`src.common.model_json.load_model_json_object` 为模型控制输出提供统一、有限的 JSON 对象提取规则。它只使用 Python 标准库，返回一个对象或 `None`；它不验证能力 schema、业务字段、权限或调用方的授权条件。

解析器允许纯对象、一个对象外包 Markdown 代码围栏，或对象前后的普通说明文字。它会检查完整原文中的容器边界：最多 65,536 个 Unicode 码位、容器深度最多 64（根对象为第 1 层），且只能有一个最外层 `{}` 或 `[]` 容器。根值必须是对象。对象内的数组和对象可正常嵌套，双引号字符串中的括号、花括号和转义引号不会被当作边界。

重复键（包括转义后相同的键）、`NaN`、正负无穷、溢出为无穷的浮点数、畸形结构、多余的第二个容器和超限输入都会被拒绝。解析器不会截断或修补输出，也不会从数组、坏掉的外层对象或多段代码围栏中挑选一个看起来可用的对象。未加引号的说明性方括号（例如 `[final]`）也算另一个容器，因此会保守拒绝。

三个旧包装器继续保留各自入口：能力路由 `load_json_object`、联网搜索 `_load_json` 和预约解析 `_extract_json`。有效对象的字段归一化、目录校验、开关和 veto 仍由原业务逻辑负责。预约解析在严格 JSON 拒绝后仍会进入现有正则回退；若回退识别出有效时间，它仍会返回 `is_reminder=True`、置信度 `0.75` 且 `parsed_by_llm=False`。因此严格 JSON 约束本身不能保证完全没有误触发提醒。

从 `NachoBot` 目录运行针对性验证：

```powershell
py -3.11 -B -m unittest discover -s tests -p test_model_json.py
py -3.12 -B -m unittest discover -s tests -p test_model_json.py
```
