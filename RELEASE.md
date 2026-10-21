# OpenCode Session Viewer v1.0.0

## 版本说明

这是 OpenCode Session Viewer 的首个正式发布版本，提供了完整的会话浏览与索引体验。

## 功能亮点

- 按创建时间浏览所有 OpenCode session
- 分页查看会话列表，支持 10 / 20 / 50 条每页
- 查看最近 10 次用户输入与上下文回溯
- 展示 token 用量、成本、agent、model 等元信息
- 以独立路径方式显示文件位置，并支持复制和打开目录
- 使用纯 Python 标准库实现，无额外依赖
- 提供明暗双主题界面

## 使用方式

```bash
python server.py
```

默认会读取当前用户的 OpenCode 数据库路径，或通过环境变量 `OPENCODE_DB` 覆盖。

## 备注

- 适用于 OpenCode 数据库文件
- 纯本地 Web 界面，便于快速排查会话历史
- 本版本为首次稳定发布
