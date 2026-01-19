# 使用说明
发送B站视频链接到群里，麦麦会自动解析并发送视频。
还有问题加qq3087033824
觉得好用的话，可以点个star
### 请务必仔细填写config.toml！！！！！
### 如果你不知道什么是wsl，请务必保证wsl转换为false！！！
## 使用方法

1. 下载本插件。
2. 将插件解压到麦麦的 `plugins` 目录。
3. 下载 [ffmpeg](https://ffmpeg.org/)。
4. 解压 ffmpeg。
5. 将解压后的 ffmpeg 文件夹放到 `bilibili_video_sender_plugin` 目录下。
6. 打开 `config.toml`，填入 `sessdata` 和 `buvid3`（获取方法见下方）。
7. 在napcat上新建一个正向http,并在config.toml内填入端口
8. 使用愉快 😊。

---

## sessdata 和 buvid3 获取方法

1. 使用 Chrome 浏览器打开 B站主页。
2. 按下 `F12` 打开开发者工具。
3. 点击顶部的 `Application`（应用）选项卡。
4. 按 `F5` 刷新页面。
5. 在左侧栏找到 `Cookies` 并展开。
6. 找到 `bilibili` 相关的 Cookie 并点击。
7. 在右侧的 `Value` 列找到 `sessdata` 和 `buvid3` 的值。
8. 将这两个值填入 `config.toml` 文件中对应的位置。

### 参考截图

- 开发者工具打开界面  
  ![开发者工具界面](https://github.com/user-attachments/assets/d8b040de-a038-4772-b588-26df92d5ce73)

- Application 栏  
  ![Application 栏](https://github.com/user-attachments/assets/0b8a5954-d6cd-47b6-95b9-126115203907)

- Cookie 位置  
  ![Cookie 位置](https://github.com/user-attachments/assets/4dc9c217-f78d-4d68-bb00-71ace2d3381f)

- bilibili Cookie  
  ![bilibili Cookie](https://github.com/user-attachments/assets/d82e3b15-64cd-490b-8eea-c6258ca0f6e2)

- sessdata 和 buvid3 示例  
  ![sessdata 和 buvid3](https://github.com/user-attachments/assets/607aa291-c927-4d00-8975-5e85fa0d1214)

---
### napcat配置和config.toml
<img width="645" height="749" alt="image" src="https://github.com/user-attachments/assets/223c491f-8433-4c47-923a-c4c830c9e572" />
<img width="1186" height="807" alt="image" src="https://github.com/user-attachments/assets/10c79e45-048a-46c8-8d1d-ca7a4044070c" />
两个端口要保持一致

### 务必仔细填写config.toml!!!!!!


## 完成后的文件夹结构示例
<img width="412" height="131" alt="image" src="https://github.com/user-attachments/assets/63ef60df-99f3-4c79-b124-da566fd15cd0" />
<img width="659" height="182" alt="image" src="https://github.com/user-attachments/assets/ddeb422f-b9fc-49b6-a652-866d06eb812c" />



