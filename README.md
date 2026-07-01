# OpenCode Session Viewer

浏览 OpenCode 全部会话历史的轻量 Web 工具，支持分页、用户输入回溯。

## 功能

- 📋 按创建时间浏览所有 session（分页 10 / 20 / 50）
- 💬 展开查看每个 session 最近 10 次用户输入
- 💰 展示 token 用量、成本、agent、model 等元信息
- ⚡ 纯 Python 标准库，零依赖

## 快速开始

```bash
# 默认端口 8080，自动检测 opencode.db 路径
python server.py

# 指定端口和数据库路径
python server.py --port 8765 --db "C:\path\to\opencode.db"
```

浏览器打开 `http://localhost:8080` 即可。

## 数据库路径

默认读取 `%USERPROFILE%\.local\share\opencode\opencode.db`。

可通过环境变量 `OPENCODE_DB` 覆盖。

## 技术栈

- **后端**: Python 3 + http.server + sqlite3（全标准库）
- **前端**: 原生 HTML/CSS/JS，暗色主题

## 截图

![screenshot](screenshot.png)

## License

MIT
