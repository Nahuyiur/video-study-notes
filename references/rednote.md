# 小红书 / RedNote 视频来源

支持单篇视频笔记的 `https://www.xiaohongshu.com/explore/NOTE_ID`、`/discovery/item/NOTE_ID`，以及 xhslink.cn / xhslink.com 分享短链。短链只在允许的平台域名内有界解析。图文笔记、用户主页、合集、直播和批量抓取不在此来源适配中。

## 访问方式

默认只取公开页面。页面要求登录或验证时立即停止，不自动登录、绕过或循环重试。

你可以显式连接自己已安装的 [xiaohongshu-skill](https://github.com/DeliciousBuding/xiaohongshu-skill)。该 skill 和系统 Chrome 需要已配置好；本产品不安装它，也不迁移浏览器 profile。读取桥使用正常可见 Chrome 和该 skill 的独立 main 会话，只读取目标笔记，不加载评论、不互动或发布。外部 skill 缺少安全读取接口时返回不可用。

本地网页启动：

```text
uv run --python 3.12 --with pillow python scripts/video_notes.py serve --rednote-skill PATH_TO_XIAOHONGSHU_SKILL --open
```

未配置外部 skill 的机器仍可使用匿名来源、B站、YouTube、本地 CLI 和已生成笔记。平台自身的访问权限会影响哪些小红书视频可取到。

## 临时访问链接

档案只保留无查询参数的 canonical 笔记链接。网页将原始访问链接与密钥通过当前任务的标准输入传给隔离进程。两者均不写入本产品的任务配置或网页应用浏览器存储。外部 skill 与 Chrome 在使用者本机自行管理会话和浏览历史，均不随代码分发。签名媒体地址只保存在该次分析的内存上下文，结束后丢弃；HTTP/FFmpeg/外部 skill 的原始错误不展示或落盘。

Python 中可直接传入内存中的访问链接：

```python
from video_notes import analyze

result = analyze(
    canonical_url, run_dir=run_dir, provider=provider,
    rednote_access_url=current_share_url,
    rednote_skill=optional_external_skill_directory,
)
```

CLI 的 `--video` 使用不带查询参数的笔记身份。若访问需要带令牌的当前链接，使用 `--rednote-access-stdin` 从标准输入读取 `{"url":"CURRENT_LINK"}`；不要把含令牌的链接写入 shell 命令、文件、环境变量或 Git。`prepare`、`frames`、`transcribe`、`analyze` 和 `analyze-prepared` 均可提供这个临时输入与可选 `--rednote-skill PATH`。分开的进程不能复用上次内存里的访问链接，需明确重新提供；同一 Python `source_access` 上下文中的 ASR/抽帧复用已验证素材，不重复打开浏览器。

## 理解与证据

只有视频类型、合法媒体流和正有限时长通过检查后才继续。媒体流保留常规 User-Agent/Referer，不携带浏览器 Cookie。当前来源没有可靠的时间戳字幕接口，因此使用共享本地 ASR；笔记标题、描述与封面从不伪装成讲解或视频画面。理解仍来自音轨加实际抽帧，继承每次最多 30 分钟、文字/画面/调用预算、未知用量与冻结证据的规则。

小红书目前没有已验证的原生秒数跳转，输出保留原视频时间戳并链接笔记页；不声称点击可以直接跳到该秒。HTML、Markdown 和授权飞书交付复用同一个 StudyNote。

## 验证范围

来源解析、受限跳转、登录/验证码停止、媒体选择和令牌不落盘有离线测试。2026-10-09 验证了一个实际分享链接的受限跳转与登录弹窗停止。用户选择不登录，本轮没有完成该视频的音轨转写、抽帧或总结验收；可读元数据不等于素材理解通过。不能将 fixture 通过当成所有视频均可访问。Windows 文件锁有分支模拟检查，浏览器读取桥尚未在 Windows 真机验收。
