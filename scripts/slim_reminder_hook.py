#!/usr/bin/env python3
# data-slim reminder hook for the UserPromptSubmit event.
#
# WorkBuddy/CodeBuddy hook contract:
#   - Receives the hook input JSON on stdin.
#   - For UserPromptSubmit, stdout (non-JSON) is injected into the Agent's
#     context, so a printed reminder is something the Agent can act on.
#   - We always exit 0, so the user's message is NEVER blocked.
#
# Behavior: if the submitted prompt references a data file
# (pdf/csv/xlsx/json/txt/md/tsv/log/docx/pptx/...), print a soft reminder
# suggesting "帮我瘦身" (data-slim). Local files are stat'd and only reminded
# when >= 1 MB to avoid nagging on trivial files.

import sys
import os
import re
import json

DATA_EXT = r"(?:pdf|csv|xlsx?|xlsm|json|jsonl|txt|md|markdown|tsv|log|docx?|pptx?)"
THRESHOLD = 1 * 1024 * 1024  # 1 MB

# 匹配 @/path/a.pdf、/Users/x/b.csv、./c.json、~/d.txt 或裸名 report.pdf
# 注意：路径字符类限定为 ASCII，避免 \w 在 Python3 默认匹配中文导致把
# "读一下 /tmp/x.pdf" 整段吞成路径而误判。
REF_RE = re.compile(
    r"@?((?:/|\~/|\./|\.\./|[A-Za-z0-9._\-]+/)?[A-Za-z0-9._\-/ ]+\." + DATA_EXT + r")\b",
    re.I,
)

URL_PREFIXES = ("http://", "https://", "ftp://")


def is_url(tok):
    return tok.lower().startswith(URL_PREFIXES)


def remind(name):
    msg = (
        "[data-slim 提示] 检测到数据文件「%s」。"
        "建议先说「帮我瘦身」调用 data-slim 技能，在文件进上下文前压缩/裁剪，显著省 token。"
        % name
    )
    sys.stdout.write(msg + "\n")


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        prompt = data.get("prompt", "") if isinstance(data, dict) else ""
    except Exception:
        return
    if not isinstance(prompt, str) or not prompt:
        return

    for tok in REF_RE.findall(prompt):
        t = tok.strip().lstrip("@").strip().rstrip(".,;:)")
        if not t or is_url(t):
            continue

        # 本地可 stat 的路径
        if t.startswith(("/", "~")) or "/" in t:
            p = os.path.expanduser(t) if t.startswith("~") else t
            try:
                # 仅当本地存在且 >= 阈值才提醒；小文件或路径不存在均静默
                if os.path.isfile(p) and os.path.getsize(p) >= THRESHOLD:
                    remind(t)
                return  # 找到首个路径即止
            except OSError:
                return
        else:
            # 裸文件名（如 report.pdf），视为附件引用
            remind(t)
            return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
