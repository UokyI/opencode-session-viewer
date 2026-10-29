# OpenCode Session Viewer v1.1.0

## 版本说明

本次发布新增会话分支（fork）与在 opencode 中直接打开会话的能力，并加强了本地服务的稳定性与安全性。

## 新增功能

- **按输入 fork 会话**：每条用户输入右侧新增 fork 按钮，可基于该输入创建分支会话，新会话包含从会话开始到该输入为止的完整上下文
- **在 opencode 中打开**：每个会话新增终端按钮，点击后自动打开新终端并恢复该会话（`opencode --session <id>`）
- **自动启动 opencode 服务**：fork 时按需启动本地 opencode 服务，无需手动准备

## 改进

- 页面禁用缓存，更新后刷新即可看到最新界面
- Windows 下禁止多个查看器实例共用同一端口，避免旧进程返回旧页面
- 打开文件夹、fork、打开会话等本地操作仅允许本机同源请求

## 使用方式

```bash
python server.py
```

默认会读取当前用户的 OpenCode 数据库路径，或通过环境变量 `OPENCODE_DB` 覆盖。

## 备注

- fork 与打开会话功能需要已安装 opencode CLI 并加入 `PATH`
- 仅适用于 OpenCode 数据库文件
- 纯本地 Web 界面，便于快速排查会话历史
