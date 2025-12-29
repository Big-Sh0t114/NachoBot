# 对于`message_data_model.py`中`class ReplyContent`的规划解读

分类讨论如下：
- `ReplyContent.TEXT`: 单独的文本，`_level = 0`，`content`为`str`类型。
- `ReplyContent.IMAGE`: 单独的图片，`_level = 0`，`content`为`str`类型（图片base64）。
- `ReplyContent.EMOJI`: 单独的表情包，`_level = 0`，`content`为`str`类型（图片base64）。
- `ReplyContent.VOICE`: 单独的语音，`_level = 0`，`content`为`str`类型（语音base64）。
- `ReplyContent.HYBRID`: 混合内容，`_level = 0`
    - 其应该是一个列表，列表内应该只接受`str`类型的内容（图片和文本混合体）
- `ReplyContent.FORWARD`: 转发消息，`_level = n`
    - 其应该是一个列表，列表接受`str`类型（图片/文本），`ReplyContent`类型（嵌套转发，嵌套有最高层数限制）
- `ReplyContent.COMMAND`: 指令消息，`_level = 0`
    - 其应该是一个列表，列表内应该只接受`Dict`类型的内容

未来规划：
- `ReplyContent.AT`： 单独的艾特，`_level = 0`，`content`为`str`类型（用户ID）。

内容构造方式：
- 对于`TEXT`, `IMAGE`, `EMOJI`, `VOICE`，直接传入对应类型的内容，且`content`应该为`str`。
- 对于`COMMAND`，传入一个字典，字典内的内容类型应符合上述规定。
- 对于`HYBRID`, `FORWARD`，传入一个列表，列表内的内容类型应符合上述规定。

因此，我们的类型注解应该是：
```python
from typing import Union, List, Dict

ReplyContentType = Union[
    str,  # TEXT, IMAGE, EMOJI, VOICE
    List[Union[str, 'ReplyContent']],  # HYBRID, FORWARD
    Dict  # COMMAND
]
```

现在`_level`被移除了，在解析的时候显式地检查内容的类型和结构即可。

`send_api`的custom_reply_set_to_stream仅在特定的类型下提供reply)message