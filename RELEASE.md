# OpenCode Session Viewer v1.2.1

## 版本说明

本次发布优化多数据库来源的命名、来源标识与设置入口图标。

## 改进

- 数据库名称改为中文顺序命名：默认、拟1、拟2、拟3……
- 会话卡片右上角新增倾斜的数据库来源标签，按数据库顺序从黑色渐变到暗灰色，白色文字
- 标签颜色会随明暗主题自动调整
- 修正设置按钮图标，改为齿轮样式

## 使用方式

```bash
python server.py
```

默认会读取当前用户的 OpenCode 数据库路径，或通过环境变量 `OPENCODE_DB` 覆盖。

多数据库设置保存在 `%USERPROFILE%\.local\share\opencode\session-viewer.json`，可通过环境变量 `OPENCODE_VIEWER_SETTINGS` 指定其他位置。

## 备注

- fork 与打开会话功能需要已安装 opencode CLI 并加入 `PATH`
- 自定义数据库需要是包含 `session` 表的 OpenCode 数据库文件
- 纯本地 Web 界面，便于快速排查会话历史
