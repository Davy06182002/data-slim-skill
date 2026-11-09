# Changelog

## v1.0.8（2026-11-09）· 本仓库通用版

- **路径跨平台**：`SKILL.md` 中 2 处 `~/.workbuddy/skills/data-slim/scripts/slim.py` 硬编码改为相对路径 `scripts/slim.py`，并补「工作目录 = 技能包根目录」约定。上传到任何支持 `SKILL.md` 规范的 Agent 均可直接运行，不再依赖特定客户端安装目录。
- **新增「运行环境（调用前先判断）」章节**：明确本地电脑 / 云电脑 + 上传文件 / 云电脑 + 本机路径 / 跨设备调度四种情形；云端读不到本机路径时给出两条可执行出路，不把排查成本丢给使用者；写入零联网红线与隐私定位。
- `description` 补一句环境说明：本地型技能，云电脑模式仅能处理本次任务上传或云盘文件。
- 触发检查清单前加「先过运行环境判断」前置步骤。
- 新增本仓库配套文件：`README.md`、`LICENSE`(MIT)、`.gitignore`、`CHANGELOG.md`。

> 功能与 v1.0.7 完全一致，仅改路径写法、环境说明与仓库配套文档。**原 WorkBuddy 技能包未改动**。

## v1.0.7 · SkillHub 上架版

- SkillHub 发布基线，slug `data-slim-token-davy`，canonicalName `@user_7a0b33e7/data-slim-token-davy`。
- 四层压缩 L1–L4、七类命令、账本与里程碑、默认关闭的显式开关。
- 命令内含安装目录绝对路径，仅适配当时的客户端目录结构。
