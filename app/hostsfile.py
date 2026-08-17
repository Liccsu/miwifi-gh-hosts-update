"""GitHub-IP-hosts 数据源解析与路由器 hosts 合并。

与小米路由器自定义 Hosts 页面逻辑保持一致:
- 每行去掉 `#` 之后的内容, 空行删除
- 连续空白合并为单个空格
- 行必须至少包含 "IP 域名" 两列
"""

import re

# 小米自定义 Hosts 页面的写入长度上限 (字符), 来自前端 index.js 的校验
MAX_HOSTS_LEN = 35840

_SPACES_RE = re.compile(r"\s+")


def parse_hosts(content):
    """解析 hosts 文本为规范化条目列表 (每行一条 "IP 域名")。

    丢弃注释行、空行与不足两列的行。规范化为单空格分隔。
    """
    entries = []
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        line = _SPACES_RE.sub(" ", line)
        parts = line.split(" ")
        if len(parts) < 2 or not parts[1]:
            continue
        entries.append(line)
    return entries


def normalize_entries(entries):
    """对已存在的条目做与 parse_hosts 相同的规范化 (不去重、不删行)。"""
    result = []
    for raw in entries:
        if not isinstance(raw, str):
            continue
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        line = _SPACES_RE.sub(" ", line)
        if len(line.split(" ")) < 2:
            continue
        result.append(line)
    return result


def merge(existing, managed):
    """合并路由器现有条目与托管条目。

    managed 中出现的域名会被整体替换为最新内容, 其余 (用户手动添加的)
    条目原样保留在最前。返回合并后的完整列表。
    """
    managed_domains = set()
    for line in managed:
        parts = line.split(" ")
        if len(parts) >= 2:
            managed_domains.add(parts[1])

    existing = normalize_entries(existing)
    keep = []
    for line in existing:
        parts = line.split(" ")
        if len(parts) >= 2 and parts[1] in managed_domains:
            continue
        keep.append(line)
    return keep + managed


def total_length(entries):
    """条目以换行拼接后的总字符数, 与前端校验口径一致。"""
    return len("\n".join(entries))
