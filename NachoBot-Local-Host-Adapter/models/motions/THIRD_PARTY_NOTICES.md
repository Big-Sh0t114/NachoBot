# 第三方动作资源与授权

本目录的 `aist/*.t2.json` 是供本机 VRM 主播直接播放的 60 FPS 真人舞蹈动作。它们不是模型文件，也不包含人物外观、视频或音乐。

## AIST++ Dance Motion Dataset

- 原始数据集：AIST++ Dance Motion Dataset，Google LLC
- 数据集主页：https://google.github.io/aistplusplus_dataset/
- 授权：Creative Commons Attribution 4.0 International（CC BY 4.0）
- 授权正文：https://creativecommons.org/licenses/by/4.0/
- 论文：Ruilong Li, Shan Yang, David A. Ross, Angjoo Kanazawa. *AI Choreographer: Music Conditioned 3D Dance Generation with AIST++*. ICCV 2021.
- 本项目使用内容：由 AIST++ SMPL 舞蹈标注重定向得到的 VRM humanoid 本地四元数与髋部轨迹。

动作转换后的紧凑 JSON 取自 MotionMind 固定提交：

```text
https://github.com/rohitacharyams/MotionMind/tree/ab5d0efc904d856f65c69de7f4e4eabd22e6ec49
```

MotionMind 的转换和播放器代码采用 MIT License；其第三方动作数据继续遵守 AIST++ 的 CC BY 4.0。当前文件映射如下：

| 本地文件 | AIST++ 序列 | SHA-256 |
| --- | --- | --- |
| `aist/cute.t2.json` | `gJB_sBM_cAll_d08_mJB5_ch04` | `4b97c8cbdb288bb851a7dcea8bf8f4316c6cef17d92bc83f791644edafd2d512` |
| `aist/energetic.t2.json` | `gJS_sBM_cAll_d01_mJS3_ch05` | `b10abaf476552b251ca01d8f3e8f09e71f240e71df13ed4249bd4ac7f8b3f919` |
| `aist/kpop.t2.json` | `gLH_sBM_cAll_d17_mLH4_ch08` | `8bb716934574dbb4a9efb5c53e8e5308e2bc1ccc0880b5c4fe6772aff7cf6282` |
| `aist/hiphop.t2.json` | `gMH_sBM_cAll_d22_mMH3_ch04` | `545e4999262ed7a4d101655e03512b6fe6bcd7a55d5610fd5e075430098446f4` |
| `aist/shuffle.t2.json` | `gHO_sBM_cAll_d20_mHO4_ch01` | `0f6764f2d0c92d8f7aa238ba2f68379c833b70132e92c9ec3d526bb449fa202c` |
| `aist/elegant.t2.json` | `gWA_sBM_cAll_d26_mWA0_ch05` | `bc5f782fe04cb872f536e207ac228ee8ece93b5b2cb2ca5f811c40b447a40d51` |

重新分发或公开直播使用这些动作时，请保留本文件中的 AIST++ 署名与 CC BY 4.0 链接。

## WAVEFILE MMD Motion（本地测试样例）

- 原始动作：WAVEFILE MikuMikuDance motion data（`wavefile_v2.vmd`）
- 原始发布说明：https://designheritage.mit.edu/_dh_debug/THREEjs_122/examples/models/mmd/vmds/readme_wavefile.txt
- 原始来源视频：`sm13147122`；原曲：`sm11938255`
- 本项目使用内容：通过 AnyaDance 官方 Blender 导出器和 MMD Tools 转换为 `anyadance/cute.json` 的 solved world-space joints；原始 VMD 同目录保留为转换输入。
- 原作者说明：允许修改和再发布；商业使用需先联系作者，禁止未经授权的商业使用。使用时请保留原始说明并遵守作者要求。
- 本地文件 | SHA-256 |
  | `anyadance/wavefile_v2.vmd` | `9CF9264CCBEFCC2C4C10175BBC66270B1DE2A392A08D11BD0B3D233B6B737CBF` |
  | `anyadance/cute.json` | `C1469BCECE285B418211E7081AB2E1A42AB35FE0B1914191A0C51E7DDD68A438` |
