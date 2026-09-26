[English](README.md) | [**简体中文**](README.zh-CN.md)

# Monash Moodle Downloader

一个 Python 命令行工具，用于保存本人有权访问的 Monash Moodle 课程文字和非媒体文件，
方便离线学习。它结合 Moodle AJAX 课程数据和浏览器可读取的页面，因此不依赖 Moodle REST
Token，也不依赖某一种固定的课程页面布局。

程序自己写出的菜单、进度和结果标签使用英文。浏览器会请求澳大利亚英语（`en-AU`）；
Moodle 课程标题和正文保留来源语言。中文版 README 说明的是同一组命令，不会切换程序的运行语言。

## 保存内容

- 将 Week 标题以及有用的课程、Page、Text and media 和 Assignment 文字保存为 Markdown。
- 下载 File、Folder 和 Assignment 中的附件，例如 PDF、Office、CSV、文本、代码、JAR 和 ZIP。
- 生成带版本的 `<课程代码> - Last Sync.json`，记录最近一次检测到的结构和结果。
- 将外部链接保存为引用；如果确认链接直接指向非媒体文件，则可能下载该文件。

图片、视频、音频、字体、Canva、Panopto、YouTube、H5P 和普通外部网页不会下载。
ZIP 文件只保存而不解压，下载的文档也不会转换格式。

## 环境要求与安装

- 装有 Google Chrome 的 macOS
- Python 3.12 或更新版本
- [uv](https://docs.astral.sh/uv/)

```console
git clone https://github.com/ruixiangjin/monash-moodle-downloader.git
cd monash-moodle-downloader
uv sync --all-groups
uv run mmd --help
```

可以在仓库目录中使用 `uv run mmd ...`；如果希望在任何位置使用较短的 `mmd ...` 命令，
可以执行一次 `uv tool install .`。

## 首次登录

```console
uv run mmd login
uv run mmd doctor
uv run mmd courses
uv run mmd menu
```

`login` 会打开一个独立的 Chrome 窗口。请只在该窗口中完成 Monash SSO 和 MFA。
本工具绝不会接收你的密码或验证码。你可以随时运行 `mmd login`，创建或更新保存的会话。

`courses` 和 `menu` 会显示两组课程：当前显示在 Moodle Dashboard 上的课程，以及之前标记为
**Remove from view** 的课程。被移除显示的课程仍然可以手动选择。

## 交互式终端菜单

运行 `uv run mmd menu`，或者在 Finder 中双击 `Monash Moodle Downloader.command`。
启动器会根据自身位置找到仓库，因此其中不包含用户专属路径。如果保存的会话不存在或已经
过期，菜单会自动打开 Chrome 供你完成 Monash SSO/MFA，并在登录成功后继续。

菜单提供以下选项：

1. 增量同步所有当前课程（不包括 Remove from view 的课程）。
2. 选择一门当前课程或 Remove from view 的课程，然后同步整门课程。
3. 选择一门课程，然后输入一个或多个可用 Week，例如 `3`、`3-5`、`3,7-8` 或 `3，7～8`。
4. 选择一门课程，然后只同步所有 Week 之外的 General 内容。

菜单执行的增量同步与命令行选项相同。“所有当前课程”操作不会自动包含 Remove from view
的课程。程序会先读取课程实际存在的 Week 列表再接受选择；如果存在 Week 0，也会支持。
每次操作后菜单都会保持打开，直到你在主菜单中明确选择 `0 Exit`。如果课程中存在 Week 0，
`scan` 和 `sync` 命令也可以使用 `--week 0`。

General 可能包含评估信息、作业、课程信息，或 Moodle 放在教学 Week 以外的其他资料。
如果一项 Assignment 位于某个 Week 内，它仍归属于该 Week，而不会被放入 General。

## 扫描与同步

```console
# 读取结构和文字，但不下载附件文件内容
uv run mmd scan --course FIT2014

# 增量同步一门课程或一个教学 Week
uv run mmd sync --course FIT2014
uv run mmd sync --course FIT2102 --week 3

# 同步所有当前课程，不包括 Remove from view
uv run mmd sync --all

# 忽略远端元数据，重新获取文件内容
uv run mmd sync --course FIT2014 --refresh
```

普通同步必须且只能指定一个 `--course`；只有明确使用 `--all` 才会选择所有课程。
课程代码和 Moodle 数字课程 ID 均可使用。

默认输出目录是 `~/Desktop/Monash Moodle Downloads`，结构如下：

```text
课程名称/
├── 课程名称.md
├── 课程代码 - Last Sync.json
├── Week 01 - 标题/
│   ├── Week 1 - 标题.md
│   ├── Files/
│   └── Assignments/
│       └── 作业名称/
│           └── 作业名称.md
└── General/
    └── General.md
```

在 `scan` 或 `sync` 命令中使用 `--output /其他/文件夹`，可以选择不同的资料目录。
生成的 Markdown 文件使用第一个标题作为文件名。下次扫描或同步时，工具会安全迁移能够识别的
旧版 `README.md` 和 `manifest.json`。无法识别的文件，或内容不同且发生冲突的文件，会保留并
显示警告。

## 增量行为

私有 SQLite 状态会记录 ETag、Last-Modified、大小、SHA-256 和本地路径。之后同步时，
程序会先使用远端元数据判断，再决定是否请求文件内容：

- 本地未变化的文件不会重复下载；
- 已变化的文件和被手动删除的本地文件会重新下载；
- 中断的传输使用临时 `.part` 文件，这些文件会被移除，不会伪装成完整文件；
- 从 Moodle 移除的文件会被标记为 `missing_remote`，但不会删除本地副本；
- `--refresh` 会有意绕过“未变化”检查。

每次同步都会汇总已下载、未变化、已跳过媒体、不支持、缺失和失败的资源。
生成的 Markdown 使用相对路径链接到已下载文件。

扫描和同步期间，交互式终端会持续显示课程加载、页面和活动读取、外部文件链接与资源检查、
以及输出写入的进度。已知总数时会显示项目数量；重定向输出使用普通英文进度行。
使用 `--all` 时，如果某门课程失败，批量同步会在该课程停止；先前已完成的输出仍会保留。

## 隐私与仓库安全

浏览器状态、Cookie 和 SQLite 数据保存在 macOS Application Support 中。下载的资料位于仓库
之外；`.gitignore` 会排除常见凭据、数据库、未完成文件和输出目录。导出的清单会移除常见的
Token 和临时签名查询参数。

发布更改前仍应检查 `git status`，不要提交课程资料或登录数据。只访问你自己的 Monash
账户有权使用的资料。

## 故障排查

- **需要登录：** `menu` 会自动打开 Chrome。其他命令需要运行 `uv run mmd login`，完成
  SSO/MFA 后重试。
- **找不到课程：** 运行 `uv run mmd courses`，使用其中显示的课程代码或数字 ID。
- **链接已记录但没有下载：** 它可能是 HTML、媒体、Canva、Panopto、H5P，或需要再次登录的
  外部页面。这是首个版本的预期行为。
- **文件下载失败：** 重试同步。已完成的文件会保持完整，未完成的 `.part` 文件会被移除。
- **需要完全重新获取：** 添加 `--refresh`；这会消耗更多网络流量。

## 开发检查

GitHub Actions 只运行匿名离线 fixture 和模拟 HTTP 响应。它绝不会登录 Monash，也不会下载
真实课程资料。

```console
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
```
