# PDF to Markdown Converter

将 PDF 讲义快速转换为 Markdown，使用 [marker-pdf](https://github.com/VikParuchuri/marker) + 可选 API 增强。

## 快速开始

```bash
# 1. 安装环境
make setup

# 2. (可选) 配置 API key
export OPENROUTER_API_KEY=sk-or-v1-your-key-here

# 3. 转换
make convert
```

运行后会弹出交互菜单：

```
Select mode:
> A) Direct output (no API)
  B) API format cleanup
  C) API understand + rewrite
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
| `make convert` | 批量转换（弹菜单） |
| `make convert-one F=xxx.pdf` | 单文件转换（弹菜单） |
| `make compare` | 对比 output/ vs md_ref/ |
| `make clean` | 清理产出 |
| `make setup` | 安装环境 |

## 目录结构

```
pdf/       → 输入 PDF 文件
md_ref/    → 参考 Markdown
output/    → 转换产出（自动创建）
```
