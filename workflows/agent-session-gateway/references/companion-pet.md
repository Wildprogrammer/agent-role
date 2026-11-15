# 伴随屏桌宠表现层

电脑伴随屏和手机终端共用 `pet.js` 与 `pet.css` 中的桌宠、状态、麦克风指示和字幕。伴随屏从设备状态和临时字幕订阅更新组件，手机终端从手机聊天连接更新组件。每个入口仍有自己的登录和交互。

## 状态映射

| 权威输入 | 动作 |
|---|---|
| neutral | 呼吸待机；每24秒轮换一次张望、伸懒腰、蝴蝶或小憩 |
| listening | 东张西望；原麦克风提示仍只由真实captureActive驱动 |
| thinking | 思考冒泡 |
| speaking | 对屏说话；声波只在真实speechActive投影下出现 |
| question | 踱步等待；确认按钮维持原prepared action语义 |
| happy | 雀跃庆祝，仅来自原状态事件，不由动画计时器伪造完成 |
| error | 垂头叹气冒汗 |
| offline | 低饱和等待，继续显示断开/等待设备文字 |
| privacy | 安静睡觉；原隐私清字幕/退订语义不变 |
| pairing | 张望 |
| updating / audio_testing | 忙碌点按；仅表现原状态，无新增维护入口 |

空闲时点击、按 Enter 或空格可轮换挥手、害羞、笑和蝴蝶。状态变化时立即卸载旧动作，先显示当前状态的轻量表情。新动画开始播放并有可用画面后才显示。网络缓冲、播放拒绝和解码失败时继续用备用表情。隐藏页面或开启减少动态效果时会释放视频。浏览器按需加载当前片段，带版本的资源 URL 允许浏览器私有缓存一天，页面与 API 响应仍使用 `no-store`。

`/static/pet-preview.html` 是独立动作预览，不调用认证或会话 API。现有静态资源可以刷新读取。HTTP 缓存策略变更在服务进程重新载入 Python 代码后生效。

## 手机端适配与部署

手机空闲且绑定已验证时使用待机动作；录音使用聆听、识别/推理使用思考、上传/合成/停止处理中使用忙碌、待选会话和等待播放使用等待、网络离线和错误各自映射。说话动画和波形由实际手机音频播放驱动，静音后立即停止说话动作。登录过期、退出和隐藏页面暂停动画；素材不可用回退原眼睛，已有字幕、拍照、重连和绑定逻辑不依赖动画成功。

手机服务以 `MOBILE_STATIC_FILES` 限定允许的静态资源。首次接入需受管重启加载Python白名单；如启用桌宠预览或可选素材，公网转发策略应按需添加相应的精确路径，不能只更新HTML或只更新局域网。保留原有API、MCP后备路由、Host重写与登录校验，不把整个静态目录或管理API泛化暴露。后续既有资源的视觉修改可以刷新生效。首次部署不需要烧录ESP32。

## 可选动画资源

基础表情、字幕和麦克风指示随 Gateway 安装。默认启用轻量表情。要使用鲸鱼娘动作，在 Gateway 源码根目录运行以下命令。

```powershell
python workflows/agent-session-gateway/scripts/install_optional_pet_assets.py
```

脚本按 [固定清单](lingxi-pet-assets.json) 下载 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) npm `0.4.7` 的14段原始 VP9-alpha WebM，校验完整长度和 SHA-256，最后写入素材清单以启用播放。安装后刷新页面。浏览器从 Gateway 同源加载，不直接访问 CDN；安装程序仅在显式执行时下载。

## 上游署名与许可

原型「溟月」：[@上善无形](https://space.bilibili.com/4456176)；DeepSeek 版女仆鲸鱼娘：[@ZipZipPipe](https://space.bilibili.com/4168597)；动画维护者：PC2005-cloud。角色与素材采用 **CC BY-NC-SA 4.0**。发布时保留该许可、署名和 [原项目链接](https://github.com/PC2005-cloud/dsh-pet)。伴随屏和手机页附有来源及非官方形象说明。

透明 WebM 以 Chromium 和 Firefox 为主要目标。部分浏览器能播放 VP9 却无法正确显示透明通道。检测不到 VP9 支持或播放失败时使用轻量表情。
