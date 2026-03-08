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
│ 📂 13 PDFs in pdf/ (14.3 MB)              │
│ 📄 540 pages total                         │
╰───────────────────────────────────────────╯

What to do?
> 1) Convert all PDFs
  2) Convert single PDF
  3) Split large PDF by chapters
  4) Compare output vs reference
```

用 ↑↓ 箭头选择，回车确认。

## 三种模式

| 模式 | 说明 | 需要 API |
|---|---|---|
| **A** | marker 本地转换 + 自动后处理 | ❌ |
| **B** | A + API 格式整理（保持内容不变） | ✅ |
| **C** | A + API 理解重写（重组内容结构） | ✅ |

## 可用模型（通过 OpenRouter）

| 模型 | 特点 |
|---|---|
| gpt-4o-mini | 快速便宜 |
| deepseek-v3.2 | 性价比高 |
| qwen2.5-72b-instruct | 中文友好 |

## 命令

| 命令 | 说明 |
|---|---|
| `make run` | **主入口** — 交互式主菜单 |
| `make convert` | 批量转换（弹菜单） |
| `make convert-one F=xxx.pdf` | 单文件转换（弹菜单） |
| `make split F=xxx.pdf` | 大 PDF 按章节拆分转换 |
| `make split-pages F=xxx.pdf P=50` | 每 50 页拆一个 |
| `make compare` | 对比 output/ vs md_ref/ |
| `make clean` | 清理产出 |
| `make setup` | 安装环境 |

## 拆分大 PDF

对于多章节的大 PDF，`make run` → 选 "Split large PDF by chapters" 支持三种拆法：

| 方式 | 说明 |
|---|---|
| **By bookmarks** | PDF 有书签时自动检测章节（最精确） |
| **By page count** | 每 N 页拆一个（`make split-pages P=50`） |
| **By headings** | 整个转换后按 `#` 标题切成多个 MD |

## 项目结构

```
main.py      → 统一入口（主菜单）
convert.py   → 核心转换（marker + 后处理）
api.py       → API 增强（OpenRouter 调用）
split.py     → 大 PDF 拆分
compare.py   → 输出对比
pdf/         → 输入 PDF 文件
md_ref/      → 参考 Markdown
output/      → 转换产出（自动创建）
```
