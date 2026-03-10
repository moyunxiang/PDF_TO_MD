# PDF to Markdown Converter

将 PDF 讲义快速转换为 Markdown，使用 [marker-pdf](https://github.com/VikParuchuri/marker) + 可选 API 增强。

## 快速开始

```bash
# 1. 安装环境
make setup

# 2. (可选) 配置 API key
export OPENROUTER_API_KEY=sk-or-v1-your-key-here

# 3. 启动
make run
```

运行后会弹出交互主菜单：

```
╭── PDF → Markdown ─────────────────────────╮
│ 📂 3 PDFs in pdf/ (14.3 MB)               │
│ 📄 540 pages total                         │
╰───────────────────────────────────────────╯

What to do?
> 1) Convert PDFs → Markdown
  2) Convert single PDF
  3) Split large PDF
  4) Enhance Markdown with API
```

## 三步工作流

### Step 1: 拆分大 PDF（可选）

```bash
make split F=textbook.pdf        # 按书签拆分
make split-pages F=textbook.pdf P=50  # 按页数拆分
```

纯 PDF 拆分，不做转换。输出到 `pdf/{name}/`（如 `pdf/textbook/ch01.pdf`）。

| 方式 | 说明 |
|---|---|
| **By bookmarks** | PDF 有书签时自动检测章节（最精确） |
| **By page count** | 每 N 页拆一个 |

### Step 2: 转换（纯本地，不花钱）

```bash
make convert              # 批量转换所有 PDF（自动扫描子目录）
make convert-one F=xxx.pdf  # 单文件转换
```

marker 本地 ML 模型提取 PDF → Markdown + 自动后处理（代码块语言标记、标题规范化等）。

> **性能优化**：float16 半精度推理 + 加大 batch size + 砍掉无用 LLM 处理器，比默认配置快 1.5-2x。

> **智能扫描**：如果 `pdf/textbook/` 下有拆分 PDF，会自动转换这些章节（跳过原件 `pdf/textbook.pdf`）。

输出到 `markdown/`，结构与 `pdf/` 对应。

### Step 3: API 增强（可选，需要 API key）

```bash
make enhance
```

扫描 `markdown/` 目录下的 MD 文件，**基于实际内容精确估算 token**，选择增强模式和模型后执行。

- **全并发**：多文件自动并行调用 API（N 个文件 = N 并发）
- **自动重试**：API 失败时指数退避重试（1s → 2s → 4s → ... → 32s）

输出到 `enhanced/`，按模式命名（如 `enhanced/math2043_cleanup/`、`enhanced/math2043_outline/`），不覆盖原始 MD，不同模式互不覆盖。

> **💡 也可以手动放 .md 文件到 `markdown/`**
> 支持两种格式：`markdown/my-notes/xxx.md`（文件夹）或直接 `markdown/my-notes.md`（单文件），enhance 都能检测到。

| 模式 | 说明 |
|---|---|
| **B** | 格式整理（保持内容不变，修复排版） |
| **C** | 理解重写（重组为学习笔记） |
| **D** | 中文教学提纲（中文骨架 + 英文术语/代码） |

| 模型（OpenRouter） | 特点 |
|---|---|
| gpt-4o-mini | 快速便宜 |
| deepseek-v3.2 | 性价比高 |
| qwen2.5-72b-instruct | 中文友好 |
| gemini-3-flash-preview | Google 最新 |

> 模型列表在 `models.json` 中配置，可随时增删。

## 命令一览

| 命令 | 说明 |
|---|---|
| `make run` | **主入口** — 交互式主菜单 |
| `make convert` | 批量转换（自动扫描子目录） |
| `make convert-one F=xxx.pdf` | 单文件转换 |
| `make split F=xxx.pdf` | 拆分大 PDF → `pdf/{name}/` |
| `make split-pages F=xxx.pdf P=50` | 按页数拆分 |
| `make enhance` | API 增强已有 MD |
| `make clean` | 清理 markdown/ 和 enhanced/ |
| `make setup` | 安装环境 |

## 目录结构

```
main.py      → 统一入口（主菜单 + CLI）
convert.py   → 核心转换（marker + 后处理）
api.py       → API 增强（独立步骤，OpenRouter）
split.py     → 大 PDF 拆分（纯拆分，不转换）
pdf/         → 输入 PDF（拆分后的子目录也在这里）
markdown/    → 纯净 Markdown（转换产��� + 可手动放入）
enhanced/    → API 增强版 Markdown（按模式命名：*_cleanup / *_rewrite / *_outline）
```
