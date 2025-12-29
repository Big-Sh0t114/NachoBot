# Notify Args
```python
Seg.type = "notify"
```
## 群聊成员被禁言
```python
Seg.data: Dict[str, Any] = {
    "sub_type": "ban",
    "duration": "对应的禁言时间，单位为秒",
    "banned_user_info": "被禁言的用户的信息，为标准UserInfo转换成的字典"
}
```
此时`MessageBase.UserInfo`，即消息的`UserInfo`为操作者(operator)的信息

**注意: `banned_user_info`需要自行调用`UserInfo.from_dict()`函数转换为标准UserInfo对象**
## 群聊开启全体禁言
```python
Seg.data: Dict[str, Any] = {
    "sub_type": "whole_ban",
    "duration": -1,
    "banned_user_info": None
}
```
此时`MessageBase.UserInfo`，即消息的`UserInfo`为操作者(operator)的信息
## 群聊成员被解除禁言
```python
Seg.data: Dict[str, Any] = {
    "sub_type": "whole_lift_ban",
    "lifted_user_info": "被解除禁言的用户的信息，为标准UserInfo对象"
}
```
**对于自然禁言解除的情况，此时`MessageBase.UserInfo`为`None`**

对于手动解除禁言的情况，此时`MessageBase.UserInfo`，即消息的`UserInfo`为操作者(operator)的信息

**注意: `lifted_user_info`需要自行调用`UserInfo.from_dict()`函数转换为标准UserInfo对象**
## 群聊关闭全体禁言
```python
Seg.data: Dict[str, Any] = {
    "sub_type": "whole_lift_ban",
    "lifted_user_info": None,
}
```
此时`MessageBase.UserInfo`，即消息的`UserInfo`为操作者(operator)的信息