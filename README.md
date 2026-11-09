# data-slim-skill

> 纯本地、零联网的文件瘦身 Skill —— 在文件进入 AI 上下文之前先压一遍，实测省 30%–60% token。

一个符合 `SKILL.md` 规范的 Agent 技能包（不是 Python 库、不是编辑器插件）。核心动作由 `scripts/slim.py` 完成：**不把文件读进对话**，直接在本机执行压缩，产出 `.slim.*` 新文件，原文件只读不动。

> ⚠️ **同名区分**：GitHub 上另有一个 `hieutrungle/data-slim`（PyTorch 科学数据压缩方向）。本仓库与该项目的作者、代码、用途均**无任何关联**，搜索时请认准 `data-slim-skill`。

---

## 一、为什么需要它

把一份需求文档、一份 CSV 丢给 AI，对话没几句上下文就快满了。问题往往不在「聊得多」，而在「喂得多」——原始文件里大量空行、重复段、冗余格式，对模型来说是纯噪声 token。

data-slim 在上传前跑一遍压缩，并配合输出端的对话纪律规则，系统性降低 token 消耗。

### 实测数据

| 场景 | 结果 |
|---|---|
| 一份需求文档 | **308 → 144 token，省 53%** |
| 不同文件的常见区间 | **省 30%–60% token** |
| 黑白转曲矢量文本 PDF | 体积可缩约 **10 倍** |
| 扫描 / 转曲 PDF（无文本层） | OCR 产出文本层后按文本读，省约 **70%–80%** 上下文 token |

> 只报省了多少 token，**不折算成钱**（不同模型计价口径不一致，折算没有意义）。

---

## 二、四层压缩

| 层 | 做什么 | 风险 |
|---|---|---|
| **L1 空白压缩** | 去多余空行、尾随空格 | 零风险，首推 |
| **L2 结构化** | CSV / JSON 去冗余、紧凑对齐 | 低，默认保留合法重复行 |
| **L3 规则去客套** | 按可配置规则删套话 | 中，规则可配 |
| **L4 语义压缩** | 在对话里把长文 / 长回复提炼成要点 | 真省上下文，产物 `.slim.md` |

**L1–L4 永久免费，无需 License**。仅面向团队 / 重度场景的「整目录批量并行 + 用量报告导出」属于 Pro。

---

## 三、安装

### 方式 A：SkillHub（国内最省事）

把下面这句发给你的 AI 助手：

```
请根据 https://skillhub.cn/install/skillhub.md，安装 @user_7a0b33e7/data-slim-token-davy
```

或在 SkillHub 搜索 `data-slim`：<https://www.skillhub.cn/skills/user_7a0b33e7/data-slim-token-davy>

### 方式 B：手动放进 Agent 的 skills 目录

把本仓库整个目录复制到你的 Agent 技能目录（例如 `~/.workbuddy/skills/data-slim/`），重启 / 刷新技能列表即可。包内命令全部是**相对路径**，不依赖任何特定安装位置。

### 方式 C：豆包 PC 客户端（个人自用）

「工作 / 工作任务」模式 →「技能」→「+ 添加」→「上传技能」→ 选 ZIP（不用解压）→ 运行环境选 **本地电脑**。不需要企业认证、不需要审核。

### 方式 D：目录站收录（建仓后）

本仓库建好后，可被 `npx skills add Davy06182002/data-slim-skill` 一类基于 Git 仓库地址的 Skill 目录站收录。仓库地址：<https://github.com/Davy06182002/data-slim-skill>

---

## 四、快速开始

> **本技能默认关闭**：需显式说「**启用瘦身 / 开启 data-slim**」才生效；未启用时完全不介入、不扫描、不自动跑。装了没反应，先检查这一步。

启用后，点名文件即可（也可直接带瘦身意图词）：

```
启用瘦身，处理这份 /path/to/台账.csv
```

等价的命令行（先把工作目录切到本仓库根目录）：

```bash
python3 scripts/slim.py auto 台账.csv 需求文档.pdf 报价单.json
python3 scripts/slim.py estimate 需求文档.md      # 只想看占多少 token
```

`auto` 按类型自动路由，一条命令吃下多个文件；已存在 `.slim.*` 产物的文件自动跳过，不重复处理、不重复记账。

### 命令一览

| 命令 | 用途 |
|---|---|
| `auto` | 批量按类型自动路由（推荐入口） |
| `csv` | 删空行空列、全角转半角、去重、裁列、截行 |
| `pdf` | 300dpi 灰度 → 阈值 175 二值化 → 1-bit 重建 |
| `ocr` | 渲染每页 → tesseract → 产出 `.slim.md` 文本层 |
| `text` | 压缩空白，可选头 75% + 尾 25% 截断 |
| `json` | minify，中文不转义 |
| `index` | 大目录生成摘要索引 `INDEX.md` |
| `estimate` | 估算 token（通用分词 + 中文优化两种口径） |
| `chat` / `dashboard` | 对话回复瘦身记账 / 查看累计省量 |

完整规则与参数见 [`SKILL.md`](./SKILL.md) 和 [`references/context_rules.md`](./references/context_rules.md)。

---

## 五、运行环境（重要）

**这是本地型技能**：核心动作是读取本机文件并执行脚本，依赖本机文件系统与本地 Python 环境。

| 环境 | 可处理 | 能力 |
|---|---|---|
| **本地电脑（推荐）** | 本机任意路径 / 目录 | 全功能 |
| 云端沙箱 + 本次上传 / 云盘文件 | 仅这些文件 | `csv` / `text` / `json` / `estimate`（仅标准库） |
| 云端沙箱 + 本机文件路径 | 读不到 | 需上传文件或切回本地电脑 |

**零联网红线**：不会为了绕过限制把文件传到任何外部服务、网盘或第三方接口；文件不出本机，隐私友好。

---

## 六、依赖

| 能力 | 依赖 |
|---|---|
| `csv` / `text` / `json` / `estimate` / `index` | **仅 Python 标准库**，装好即用 |
| `pdf` | `pymupdf`、`pillow` |
| `ocr` | 上述两项 + `tesseract`（含 `chi_sim` 语言包） |

依赖缺失时脚本会**显式报错并提示安装，不静默降级**（见 `SKILL.md`「依赖」章节）。

---

## 七、目录结构

```
.
├── SKILL.md                    # 技能定义（YAML frontmatter + 触发规则 + 工作流）
├── scripts/
│   ├── slim.py                 # 瘦身引擎主程序
│   ├── slim_license.py         # 本地 License 校验（仅 Pro 批量入口）
│   └── slim_reminder_hook.py   # 提醒钩子（部分 Agent 支持，非必需）
├── references/
│   └── context_rules.md        # 上下文纪律规则
├── LICENSE
├── CHANGELOG.md
└── .gitignore
```

---

## 八、版本

| 位置 | 版本 | 说明 |
|---|---|---|
| 本仓库 | **v1.0.8** | 相对路径跨平台 + 新增「运行环境」章节；通用 SKILL.md 包 |
| SkillHub 上架版 | v1.0.7 | 早期发布版，命令里含安装目录绝对路径，仅适配当时客户端 |

建议以本仓库 v1.0.8 为准；两者功能一致，差别只在路径写法与环境说明。

---

## 九、许可

[MIT](./LICENSE) · L1–L4 永久免费 · 零联网 · 文件不出本机
