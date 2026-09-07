#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
广西图书馆·知网全文 API  (cnki_fulltext.py)
================================================
已验证的纯 HTTP 链路（2026-09-06 实测）：
  1) login    —— 程序化登录（XOR 加密凭据），无需浏览器
  2) search   —— 程序化检索；若被知网反爬滑块拦截，抛 CaptchaError 并给出
                  verify_url，把滑块弹到浏览器人工通过一次后，同会话解锁
  3) download —— 全文 PDF 下载（bar/download 授权链 → docdown PDF）

用法：
  python cnki_fulltext.py login
  python cnki_fulltext.py search "直播电商 消费者购买意愿"
  python cnki_fulltext.py search "关键词" --pages 2 --limit 40
  python cnki_fulltext.py download <文章URL> [更多URL...] [-o 输出目录]
  python cnki_fulltext.py session-info

会话（cookies + 路由主机）保存在同目录 gxlib_session.json，跨命令复用。
下载的 PDF 默认存到 ./fulltext/，正文用 pymupdf 提取（pdftotext 对该刊 PDF 会乱码）。
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(HERE, "gxlib_session.json")
DEFAULT_OUT = os.path.join(HERE, "fulltext")
PLATFORM = "https://res.gxlib.org.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 凭据：优先读 config.json（已 gitignore，勿提交），否则用 config.example.json 占位
def _load_credentials():
    for name in ("config.json", "config.example.json"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
                if cfg.get("username") and cfg.get("password"):
                    return cfg["username"], cfg["password"]
            except Exception:
                pass
    return "", ""


USERNAME, PASSWORD = _load_credentials()

# 知网"总库"检索所需的 crossids（各库代码，取自真实检索 URL）
CROSSIDS = ("YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,"
            "BLZOG7CK,PWFIRAGL,NLBO1Z6R,NN3FJMUV")


class CaptchaError(Exception):
    """检索被知网滑块验证拦截；verify_url 供人工在浏览器中通过。"""
    def __init__(self, verify_url, detail=""):
        self.verify_url = verify_url
        super().__init__(f"滑块验证拦截: {verify_url} {detail}")


class GxlibCNKI:
    def __init__(self, session_file=SESSION_FILE):
        self.session_file = session_file
        self.cookies = {}          # name -> value
        self.hosts = {}            # 路由角色 -> host（host 不含 scheme）
        self.load_session()

    # ---------- 基础工具 ----------
    def curl(self, url, referer=None, extra_headers=None, jar_only=False,
             follow=True, out_file=None, max_redirs=8, timeout=40):
        """执行 curl（Schannel TLS 可连老式重协商的登录门户）。"""
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        args = ["curl", "-sk", "--connect-timeout", "15", "--max-time", str(timeout)]
        if cookie:
            args += ["-H", f"Cookie: {cookie}"]
        if referer:
            args += ["-e", referer]
        for h in (extra_headers or []):
            args += ["-H", h]
        if follow:
            args += ["-L", "--max-redirs", str(max_redirs)]
        if out_file:
            args += ["-o", out_file]
        args += ["-w", "\n__META__%{http_code} %{url_effective} %{size_download}"]
        args += [url]
        p = subprocess.run(args, capture_output=True)
        out = p.stdout.decode("utf-8", "ignore")
        m = re.search(r"__META__(\d+) (\S+) (\d+)\s*$", out, re.S)
        meta = m.groups() if m else (None, url, 0)
        body = out[: m.start()] if m else out
        self._absorb_set_cookie(p.stdout)   # curl -w 混在 stdout；用 http_code 判断
        return {"code": int(meta[0]) if meta[0] else 0, "url": meta[1], "size": int(meta[2]), "body": body}

    def curl_raw(self, args):
        p = subprocess.run(["curl", "-sk", "--connect-timeout", "15", "--max-time", "40"] + args,
                           capture_output=True)
        return p

    def _absorb_set_cookie(self, raw_bytes):
        """从 curl 输出中吸收 Set-Cookie 到会话（仅当响应带 -D 头时）。"""
        return  # 由显式 set_cookie() 管理，避免误解析

    def set_cookie(self, name, value):
        self.cookies[name] = value
        self.save_session()

    def load_session(self):
        if os.path.exists(self.session_file):
            try:
                d = json.load(open(self.session_file, encoding="utf-8"))
                self.cookies = d.get("cookies", {})
                self.hosts = d.get("hosts", {})
            except Exception:
                self.cookies, self.hosts = {}, {}

    def save_session(self):
        json.dump({"cookies": self.cookies, "hosts": self.hosts},
                  open(self.session_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def host_of(self, role):
        return self.hosts.get(role)

    # ---------- 1. 登录 ----------
    @staticmethod
    def _derive_key(key):
        """createkey：取 key 第 3,5,6,9,12,15,18 位字符。"""
        return "".join(key[i] for i in (3, 5, 6, 9, 12, 15, 18))

    @staticmethod
    def _xor(derived, s):
        return "".join(chr(ord(derived[i % len(derived)]) ^ ord(ch)) for i, ch in enumerate(s))

    def login(self, username=USERNAME, password=PASSWORD):
        """程序化登录，返回 True。凭据 XOR 加密（算法已从 main.js 逆向）。"""
        if not username or not password:
            raise RuntimeError("未配置账号密码：请在同目录创建 config.json（可参考 config.example.json），"
                               "填入 username / password")
        # 1) 取登录页 + 新鲜 key（cookie 写入 jar 供 POST 复用）
        jar = self._curl_jar()
        page_file = os.path.join(HERE, "_login.html")
        self.curl_raw(["-c", jar, "-o", page_file, PLATFORM + "/ermsLogin/view.do"])
        page = open(page_file, encoding="utf-8", errors="ignore").read()
        keys = re.findall(r'var\s+key\s*=\s*"([^"]+)"', page)
        if not keys:
            raise RuntimeError("登录页未取到加密 key")
        key = keys[0]
        # 2) 加密凭据（XOR 原始字节可能含 \x00，不能走 argv；先百分号编码再经 stdin 提交）
        derived = self._derive_key(key)
        payload = ("userName=" + urllib.parse.quote(self._xor(derived, username), safe="") +
                   "&password=" + urllib.parse.quote(self._xor(derived, password), safe=""))
        p = subprocess.run(
            ["curl", "-sk", "--connect-timeout", "15", "--max-time", "40",
             "-b", jar, "-c", jar, "-X", "POST", PLATFORM + "/ermsLogin/login.do",
             "--data-binary", "@-"],
            input=payload.encode("utf-8"), capture_output=True)
        resp = p.stdout.decode("utf-8", "ignore")
        self._load_jar(jar)
        try:
            data = json.loads(resp)
        except json.JSONDecodeError:
            raise RuntimeError(f"登录响应异常: {resp[:200]}")
        if data.get("status") == "error" and data.get("msg") == "hasLogin":
            # 账号已有活跃会话：同会话内 GET relogin.do 强制接管（平台单会话策略）
            p2 = subprocess.run(
                ["curl", "-sk", "--connect-timeout", "15", "--max-time", "40",
                 "-b", jar, "-c", jar, "-L", "--max-redirs", "5",
                 PLATFORM + "/ermsLogin/relogin.do"],
                capture_output=True)
            # 验证接管后会话有效（浏览页含"您好"即登录态）
            chk = subprocess.run(
                ["curl", "-sk", "-b", jar, "-c", jar,
                 PLATFORM + "/ermsClient/browse.do"],
                capture_output=True).stdout.decode("utf-8", "ignore")
            if "您好" not in chk:
                raise RuntimeError("hasLogin 接管失败")
            self._load_jar(jar)
            self.save_session()
            return True
        if data.get("status") != "ok":
            raise RuntimeError(f"登录失败: {data.get('msg')}")
        self.save_session()
        return True

    def _curl_jar(self):
        jar = os.path.join(HERE, "_gxlib_jar.txt")
        return jar

    def _load_jar(self, jar):
        if not os.path.exists(jar):
            return
        for line in open(jar, encoding="utf-8", errors="ignore").read().splitlines():
            if not line.strip() or line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) >= 7:
                self.cookies[parts[5]] = parts[6]
        if os.path.exists(jar):
            os.remove(jar)

    def _write_jar(self):
        jar = self._curl_jar()
        with open(jar, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for name, value in self.cookies.items():
                # HttpOnly 标记不影响 curl 发送；统一按普通 cookie 写
                f.write(f".res.gxlib.org.cn\tTRUE\t/\tFALSE\t0\t{name}\t{value}\n")
        return jar

    def _clean(self, s):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()

    def meta(self, article_url):
        """提取详情页结构化元数据：标题/作者/期刊/摘要/关键词/基金/分类号/在线时间/目录等。"""
        html, referer = self.fetch_article_page(article_url)
        m = {}
        def grab(pattern, flags=re.S):
            mm = re.search(pattern, html, flags)
            return self._clean(mm.group(1)) if mm else ""
        m["title"] = re.sub(r"\s*附视频$", "", grab(r'<h1[^>]*>(.*?)</h1>'))
        m["authors"] = grab(r'<h3[^>]*>(.*?)</h3>')
        # 期刊/年/卷/期/页（取自顶部来源行，如"南开管理评论 . 2020 ,23 (01) : 131-143"）
        src_txt = grab(r'class="top-tip[^"]*"[^>]*>(.*?)</p>')
        jm = re.search(r'([\u4e00-\u9fa5A-Za-z（）()0-9]{2,30})\s*\.\s*(\d{4})\s*,\s*(\d+)\s*\(([^)]+)\)\s*:\s*([\d\-]+)', src_txt)
        if jm:
            m["source"] = jm.group(1)
            m["year"] = jm.group(2)
            m["volume_issue"] = f"{jm.group(3)}({jm.group(4)})"
            m["pages"] = jm.group(5)
        else:
            m["source"] = src_txt
        # 作者单位（形如"1.中国人民大学商学院"）
        m["affiliations"] = grab(r'class="author"[\s\S]{0,400}?([\d]+[.、][^<]{2,60}(?:；|;\s)?){1,4}') or ""
        # 若作者行含上标序号，尝试单独取单位区
        af = re.search(r'<div[^>]*class="[^"]*author[^"]*"[^>]*>([\s\S]*?)</div>', html)
        if af and re.search(r'\d\s*[.、]', af.group(1)):
            m["affiliations"] = self._clean(af.group(1))[:300]
        m["abstract"] = grab(r'id="ChDivSummary"[^>]*>(.*?)</span>')
        m["keywords"] = grab(r'class="keywords"[^>]*>(.*?)</p>')
        m["funds"] = grab(r'class="funds"[^>]*>(.*?)</p>')
        m["classification"] = grab(r'class="clc-code"[^>]*>(.*?)</p>')
        m["online_date"] = grab(r'在线公开时间：</span>\s*<p[^>]*>(.*?)</p>')
        m["doi"] = grab(r'[Dd][Oo][Ii][：:]\s*<[^>]*>([^<]+)') or grab(r'DOI[：:]\s*([0-9][^\s<]*)')
        # 专辑/专题
        zj = re.search(r'专辑：</span>(.*?)</li>', html, re.S)
        zt = re.search(r'专题：</span>(.*?)</li>', html, re.S)
        m["subject"] = (self._clean(zj.group(1)) if zj else "") + (" / " + self._clean(zt.group(1)) if zt else "")
        # 文章目录
        i = html.find("文章目录")
        if i > 0:
            toc = html[i:i+8000]
            items = [self._clean(x) for x in re.findall(r'<li[^>]*>(.*?)</li>', toc, re.S)]
            m["toc"] = [x for x in items if x]
        else:
            m["toc"] = []
        return m

    def _session_valid(self):
        """browse.do 返回"您好"即当前会话有效。"""
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if not cookie:
            return False
        chk = subprocess.run(["curl", "-sk", "--max-time", "20", "-H", f"Cookie: {cookie}",
                              PLATFORM + "/ermsClient/browse.do"],
                             capture_output=True).stdout.decode("utf-8", "ignore")
        return "您好" in chk

    def ensure_cnki_hosts(self):
        """跟随知网入口，确定本会话的路由主机（首页/检索/详情/下载）。
        注意：不会自动重新登录（登录/relogin 会踢掉浏览器会话，造成互相伤害）；
        会话失效时报错并提示刷新浏览器会话后 import-cookies。"""
        if self.hosts.get("search") and self._session_valid():
            return self.hosts
        if not self._session_valid():
            raise RuntimeError(
                "当前会话无效（可能已过期或已被其他登录踢掉）。"
                "请在浏览器中重新登录图书馆平台并访问一次知网检索，"
                "然后用 import-cookies 导入新的 document.cookie。")
        # 入口重定向 → CNKI 首页（?fs=1），用 cookie 引擎跟跳以保留链上的 Set-Cookie
        jar = self._write_jar()
        home_file = os.path.join(HERE, "_home.html")
        self.curl_raw(["-b", jar, "-c", jar, "-L", "--max-redirs", "5",
                       "-o", home_file, "-w", "%{http_code}", PLATFORM + "/entry.do?rid=108&uid=361"])
        self._load_jar(jar)
        home = open(home_file, encoding="utf-8", errors="ignore").read()
        if "<title>中国知网" not in home and "检" not in home[:2000]:
            raise RuntimeError("进入知网失败（入口被拒，会话可能被踢）")
        hosts = {}
        for host, path in re.findall(r'https?://([a-z0-9]+ficg\.res\.gxlib\.org\.cn)(/[^"\'\s<>]*)', home):
            if path.startswith("/kns8s") or path.startswith("/starter"):
                hosts.setdefault("search", host)
            elif path.startswith("/bar") or "docgateway" in path or "docdown" in path:
                hosts.setdefault("download", host)
            elif path == "/" or path.startswith("/subpages"):
                hosts.setdefault("home", host)
        if not hosts.get("search"):
            # 首页里高级检索入口所在主机就是检索路由
            hosts["search"] = hosts.get("search") or hosts.get("home")
        self.hosts = hosts
        self.save_session()
        return hosts

    # ---------- 2. 检索 ----------
    # 来源类别代码（CNKI 来源标识码，2026-09 实测）
    CATEGORY_CODES = {
        "北大核心": "P01", "CSSCI": "P0209", "CSCD": "P0210",
        "AMI": "P13", "EI": "P0202", "WJCI": "P12",
    }
    SORT_CODES = {"相关度": "FFD", "被引": "CF", "下载": "DFR", "综合": "ZH", "发表时间": "PT", "时间": "PT"}

    # 检索字段代码（2026-09 对 grid 接口逐码验证；DOI 在广西图书馆代理不可用）
    SEARCH_TYPES = {
        "主题": "SU", "篇关摘": "TKA", "关键词": "KY", "篇名": "TI", "全文": "FT",
        "作者": "AU", "第一作者": "FI", "通讯作者": "RP", "作者单位": "AF",
        "基金": "FU", "摘要": "AB", "参考文献": "RF", "分类号": "CLC", "文献来源": "LY",
        "DOI": "DOI",
    }
    SEARCH_TYPE_ALIASES = {
        "subject": "主题", "theme": "主题", "keyword": "关键词", "keywords": "关键词",
        "title": "篇名", "author": "作者", "first_author": "第一作者",
        "corresponding_author": "通讯作者", "affiliation": "作者单位", "institution": "作者单位",
        "fund": "基金", "abstract": "摘要", "fulltext": "全文", "reference": "参考文献",
        "source": "文献来源", "doi": "DOI",
    }

    # 检索语言（Rlang + 数据库集；外文库与中文库 Products 中第 11 位不同 CSCF/SCSF）
    LANGUAGE_CONFIG = {
        "中文": {"rlang": "CHINESE",
                 "products": "CJFQ,CAPJ,ZHYX,CJTL,CDFD,CMFD,WBFD,CPFD,IPFD,CCND,CSCF,SCHF,SCSD,SNAD,CCJD,CCVD,CJFN"},
        "外文": {"rlang": "FOREIGN",
                 "products": "CJFQ,CAPJ,ZHYX,CJTL,CDFD,CMFD,WBFD,CPFD,IPFD,CCND,SCSF,SCHF,SCSD,SNAD,CCJD,CCVD,CJFN"},
    }

    @staticmethod
    def resolve_search_type(search_type):
        """把检索字段中文名/英文别名 → Field 代码。"""
        if not search_type:
            return "SU"
        t = search_type.strip()
        if t in GxlibCNKI.SEARCH_TYPES:
            return GxlibCNKI.SEARCH_TYPES[t]
        if t.lower() in GxlibCNKI.SEARCH_TYPE_ALIASES:
            return GxlibCNKI.SEARCH_TYPES[GxlibCNKI.SEARCH_TYPE_ALIASES[t.lower()]]
        raise RuntimeError(f"未知检索字段: {search_type}（可用：{'/'.join(GxlibCNKI.SEARCH_TYPES)}）")

    @staticmethod
    def filter_by_journals(papers, journal_list):
        """按期刊白名单过滤检索结果（用于一区/二区等知网无原生字段的筛选）。
        journal_list: 期刊名列表（或 data/q1_q2_journals.csv 的名单）。
        大小写/首尾空白不敏感匹配（CNKI 来源名可能与分区表大小写不同）。
        返回 source 命中白名单的论文。"""
        allow = {j.strip().lower() for j in journal_list if j and j.strip()}
        return [p for p in papers if (p.get("source", "") or "").strip().lower() in allow]

    def search(self, keyword, page=1, limit=20, page_size=20,
               source_categories=None, year_from=None, year_to=None,
               sort_by="被引", sort_order="desc", search_type=None,
               language="中文"):
        """程序化检索（POST /kns8s/brief/grid 数据接口），支持精准筛选。

        参数：
          keyword          检索词
          search_type      检索字段：主题(默认)/篇关摘/关键词/篇名/全文/作者/第一作者/
                           通讯作者/作者单位/基金/摘要/参考文献/分类号/文献来源/DOI
                           （DOI 在广西图书馆代理不可用，会给出提示）
          language         检索语言：中文(默认)/外文（外文检索英文文献，Rlang=FOREIGN）
          source_categories 来源类别筛选，str 或 list：北大核心/CSSCI/CSCD/AMI/EI/WJCI
          year_from/year_to 发表年份范围（如 2020 / 2024）
          sort_by           排序：被引(默认)/相关度/下载/综合/发表时间
          sort_order        desc(默认)/asc
        返回论文列表：[{title, url, authors, source, date, db, cited, download}]
        """
        field = self.resolve_search_type(search_type)
        if field == "DOI":
            raise RuntimeError("DOI 字段在广西图书馆知网代理不可用（公共站支持）。"
                               "可用 find_best_match 按题名定位，或在主题检索里直接搜 DOI 字符串。")
        lang_cfg = self.LANGUAGE_CONFIG.get(language) or self.LANGUAGE_CONFIG["中文"]
        self.ensure_cnki_hosts()
        shost = self.host_of("search")
        if not shost:
            raise RuntimeError("未找到检索路由主机")
        try:
            from curl_cffi import requests as ccr
        except ImportError:
            raise RuntimeError("需要 curl_cffi：pip install curl_cffi")
        s = ccr.Session(impersonate="chrome", timeout=40)
        s.headers.update({"Accept-Language": "zh-CN,zh;q=0.9",
                          "X-Requested-With": "XMLHttpRequest"})
        for k, v in self.cookies.items():
            s.cookies.set(k, v, domain="res.gxlib.org.cn")

        # 来源类别 → SCDBGroup(可选)
        cats = []
        if source_categories:
            if isinstance(source_categories, str):
                source_categories = [x.strip() for x in source_categories.replace("，", ",").split(",")]
            for c in source_categories:
                if c in self.CATEGORY_CODES:
                    cats.append((self.CATEGORY_CODES[c], c))
                else:
                    raise RuntimeError(f"未知来源类别: {c}（可用：{'/'.join(self.CATEGORY_CODES)}）")
        qgroups = [{"Key": "Subject", "Title": "", "Logic": 0,
                    "Items": [{"Field": field, "Value": keyword,
                               "Operator": "TOPRANK" if field == "SU" else "DEFAULT",
                               "Logic": 0,
                               "Title": next((k for k, v in self.SEARCH_TYPES.items() if v == field), "")}],
                    "ChildItems": []}]
        if year_from or year_to:
            qgroups[0]["Items"].append({
                "Field": "PT", "Value": str(year_from or ""), "Value2": str(year_to or ""),
                "Operator": "BETWEEN", "Logic": 0, "Title": "发表时间"})
        if cats:
            qgroups.append({"Key": "SCDBGroup", "Title": "", "Logic": 0, "Items": [],
                            "ChildItems": [{"Key": "LYBSM", "Title": "", "Logic": 0,
                                "Items": [{"Key": code, "Title": label, "Logic": 1,
                                           "Field": "LYBSM", "Operator": "DEFAULT",
                                           "Value": code, "Value2": "", "Name": "LYBSM",
                                           "ExtendType": 0} for code, label in cats],
                                "ChildItems": []}]})
        qj = {
            "Platform": "", "Resource": "CROSSDB", "Classid": "WD0FTY92",
            "Products": lang_cfg["products"],
            "QNode": {"QGroup": qgroups},
            "ExScope": 1, "SimpTrad": "0", "SearchType": 2, "Rlang": lang_cfg["rlang"],
            "Expands": {},
            "KuaKuCode": CROSSIDS.replace(",", ","),
            "View": "changeDBCh", "SearchFrom": 5,
        }
        sort_field = self.SORT_CODES.get(sort_by, "CF")
        sort_type = sort_order if sort_order in ("asc", "desc") else "desc"
        data = {
            "boolSearch": "false",
            "QueryJson": json.dumps(qj, ensure_ascii=False),
            "pageNum": str(page), "pageSize": str(page_size),
            "sortField": sort_field, "sortType": sort_type,
            "dstyle": "listmode", "boolSortSearch": "false",
            "productStr": "", "aside": "",
        }
        r = s.post(f"https://{shost}/kns8s/brief/grid", data=data,
                   headers={"Referer": f"https://{shost}/kns8s/search"})
        if r.status_code == 403:
            try:
                m = json.loads(r.text).get("message", "")
            except Exception:
                m = r.text
            if "/verify/" in m:
                raise CaptchaError(m, "（会话未验证：需 browser-trust 后重试）")
            raise RuntimeError(f"检索被拒(403): {m[:120]}")
        if r.status_code != 200:
            raise RuntimeError(f"检索异常 HTTP {r.status_code}")
        body = r.text
        count_m = re.search(r'([\d,]+)\s*条结果', re.sub(r"<[^>]+>", " ", body))
        total = count_m.group(1) if count_m else ""

        def _cell(row, cls):
            m = re.search(r'<td[^>]*class=["\']' + cls + r'["\'][^>]*>(.*?)</td>', row, re.S)
            if not m:
                return ""
            txt = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S)
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", txt)).strip()

        papers = []
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S)
        for row in rows:
            m = re.search(r'href="(https?://[^"]*?/kcms2/article/abstract\?[^"]+|/kcms2/article/abstract\?[^"]+)"[^>]*>(.*?)</a>', row, re.S)
            if not m:
                continue
            url = m.group(1)
            if url.startswith("/"):
                url = "https://" + shost + url
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            # 作者：按 <a> 锚点边界拆分（单元格内多个作者无分隔符）
            am = re.search(r'<td[^>]*class=["\']author["\'][^>]*>(.*?)</td>', row, re.S)
            authors = []
            if am:
                authors = [re.sub(r"<[^>]+>", "", x).strip()
                           for x in re.findall(r'<a[^>]*>(.*?)</a>', am.group(1)) if re.sub(r"<[^>]+>", "", x).strip()]
            papers.append({
                "title": title, "url": url,
                "authors": authors,
                "source": _cell(row, "source"),
                "date": _cell(row, "date"),
                "db": _cell(row, "data"),
                "cited": _cell(row, "quote"),
                "download": _cell(row, "download"),
            })
        if not papers:
            for m in re.finditer(r'href="(https?://[^"]*?/kcms2/article/abstract\?[^"]+|/kcms2/article/abstract\?[^"]+)"', body):
                papers.append({"title": "", "url": m.group(1) if m.group(1).startswith("http") else "https://" + shost + m.group(1)})
        self._last_total = total
        return papers[:limit]

    # ---------- 批量导出 / 标题匹配 / 引文格式化 ----------
    @staticmethod
    def export_papers(papers, fmt="json"):
        """论文列表批量导出为 json/csv/ris/bibtex（可导入 Zotero/EndNote）。"""
        fmt = fmt.lower()
        if fmt == "json":
            return json.dumps(papers, ensure_ascii=False, indent=1)
        if fmt == "csv":
            import csv as _csv
            import io
            buf = io.StringIO()
            w = _csv.writer(buf)
            w.writerow(["title", "authors", "source", "date", "db", "cited", "download", "url"])
            for p in papers:
                w.writerow([p.get("title", ""), ";".join(p.get("authors", [])), p.get("source", ""),
                            p.get("date", ""), p.get("db", ""), p.get("cited", ""),
                            p.get("download", ""), p.get("url", "")])
            return buf.getvalue()
        if fmt == "ris":
            out = []
            for p in papers:
                out.append("TY  - JOUR")
                for a in p.get("authors", []):
                    out.append(f"AU  - {a}")
                out.append(f"TI  - {p.get('title', '')}")
                out.append(f"JO  - {p.get('source', '')}")
                if p.get("date"):
                    out.append(f"PY  - {p['date'][:4]}")
                out.append(f"UR  - {p.get('url', '')}")
                out.append("ER  -")
            return "\n".join(out)
        if fmt in ("bib", "bibtex"):
            out = []
            for i, p in enumerate(papers, 1):
                key = f"cnki{i}"
                out.append(f"@article{{{key},")
                out.append(f"  author = {{{' and '.join(p.get('authors', []))}}},")
                out.append(f"  title = {{{p.get('title', '')}}},")
                out.append(f"  journal = {{{p.get('source', '')}}},")
                if p.get("date"):
                    out.append(f"  year = {{{p['date'][:4]}}},")
                out.append(f"  url = {{{p.get('url', '')}}}")
                out.append("}")
            return "\n".join(out)
        raise RuntimeError(f"未知导出格式: {fmt}（可用 json/csv/ris/bibtex）")

    def find_best_match(self, title, limit=30):
        """按题名检索并做字符匹配，定位/验证某篇论文是否在库。
        返回 [{title, url, source, date, ratio}]，按匹配度降序。"""
        import difflib
        papers = self.search(title, search_type="篇名", limit=limit, sort_by="相关度")
        norm = lambda s: re.sub(r"\s+", "", s or "")
        out = []
        for p in papers:
            ratio = difflib.SequenceMatcher(None, norm(title), norm(p["title"])).ratio()
            out.append({**p, "ratio": round(ratio, 3)})
        out.sort(key=lambda x: x["ratio"], reverse=True)
        return out

    @staticmethod
    def format_citation(title, authors, source, year, volume="", issue="", pages="",
                        doi="", style="gbt7714"):
        """通用引文格式化（独立于 CNKI 原始引文）。
        style: gbt7714(GB/T 7714-2015) / apa / mla / chicago / vancouver
        作者输入：字符串（; 或 ，，分隔）或列表；西文作者姓在前时按 Given Family 处理。
        """
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r"[;；,，]", authors) if a.strip()]
        styled = style.lower()
        if styled in ("gbt7714", "gbt"):
            # GB/T 7714-2015：作者等. 题名[J]. 刊名, 年, 卷(期): 页码.
            a = ",".join(authors[:3]) + (",等" if len(authors) > 3 else "")
            vol = f"{volume}({issue})" if volume and issue else (volume or issue or "")
            loc = f"{year},{vol}" if vol else str(year)
            pag = f":{pages}" if pages else ""
            return f"{a}.{title}[J].{source},{loc}{pag}."
        if styled == "apa":
            # APA 7：Author, A. A., & B. B. Author (Year). Title. Journal, Vol(Issue), pages.
            def apa_name(n):
                parts = n.split()
                if len(parts) >= 2 and not re.match(r"^[\u4e00-\u9fa5]+$", n):
                    return f"{parts[-1]}, {''.join(x[0] + '.' for x in parts[:-1])}"
                return n
            a = " & ".join(apa_name(x) for x in authors)
            vol = f"{volume}({issue})" if volume and issue else (volume or issue or "")
            loc = f"{vol}, {pages}" if vol and pages else (vol or pages or "")
            doi_s = f" https://doi.org/{doi}" if doi else ""
            return f"{a} ({year}). {title}. {source}, {loc}.{doi_s}".replace(", .", ".")
        if styled == "mla":
            # MLA 9：Author, A. A., and B. B. Author. "Title." Journal, vol. x, no. y, Year, pp. z.
            if len(authors) <= 2:
                a = ", and ".join(authors)
            elif len(authors) == 3:
                a = f"{authors[0]}, {authors[1]}, and {authors[2]}"
            else:
                a = f"{authors[0]}, et al."
            parts = []
            if volume:
                parts.append(f"vol. {volume}")
            if issue:
                parts.append(f"no. {issue}")
            if year:
                parts.append(str(year))
            if pages:
                parts.append(f"pp. {pages}")
            return f"{a}. \"{title}.\" {source}, {', '.join(parts)}."
        if styled == "chicago":
            # Chicago：Author, A. A., and B. B. Author. "Title." Journal Vol, no. Issue (Year): pages.
            if len(authors) <= 3:
                a = ", and ".join(authors) if len(authors) == 2 else ", ".join(authors[:-1]) + ", and " + authors[-1]
            else:
                a = f"{authors[0]}, et al."
            vol = f" {volume}," if volume else ""
            iss = f" no. {issue}" if issue else ""
            pag = f": {pages}" if pages else ""
            return f"{a}. \"{title}.\" {source}{vol}{iss} ({year}){pag}."
        if styled == "vancouver":
            # Vancouver：Author AB, Author CD. Title. Journal. Year;Vol(Issue):pages.
            def van_name(n):
                if re.match(r"^[\u4e00-\u9fa5]+$", n):
                    return n
                parts = n.split()
                if len(parts) >= 2:
                    return f"{parts[0]} {''.join(x[0] for x in parts[1:])}"
                return n
            a = ", ".join(van_name(x) for x in authors)
            vol = f"{volume}({issue})" if volume and issue else (volume or issue or "")
            loc = f"{year};{vol}" if vol else str(year)
            pag = f":{pages}" if pages else ""
            return f"{a}. {title}. {source}. {loc}{pag}."
        raise RuntimeError(f"未知引文风格: {style}（可用 gbt7714/apa/mla/chicago/vancouver）")

    # ---------- 3. 下载 ----------
    def fetch_article_page(self, article_url, out_html=None):
        """抓取文章详情页（curl_cffi + Chrome 指纹，反爬需指纹才放行）。"""
        self.ensure_cnki_hosts()
        try:
            from curl_cffi import requests as ccr
        except ImportError:
            raise RuntimeError("需要 curl_cffi：pip install curl_cffi")
        s = ccr.Session(impersonate="chrome", timeout=40)
        s.headers.update({"Accept-Language": "zh-CN,zh;q=0.9"})
        for k, v in self.cookies.items():
            s.cookies.set(k, v, domain="res.gxlib.org.cn")
        r = s.get(article_url, headers={"Referer": article_url.split("/kcms2")[0] + "/"})
        html = r.text
        if r.status_code != 200 or "/verify/" in r.url or "安全验证" in html[:500]:
            raise RuntimeError(f"详情页被反爬拦截 (HTTP {r.status_code})，可能信任态已过期")
        return html, article_url

    def download(self, article_url, out_dir=DEFAULT_OUT, verbose=True):
        """按文章详情页 URL 下载全文 PDF。返回 (pdf_path, title) 或抛错。"""
        self.ensure_cnki_hosts()
        html, referer = self.fetch_article_page(article_url)
        m = re.search(r'<a[^>]*href="([^"]*bar/download/order\?id=[^"]+)"[^>]*>\s*<i></i>\s*PDF下载', html)
        if not m:
            # 退而求其次：CAJ下载（与 PDF 共用令牌）或任一 bar 链接
            m = (re.search(r'<a[^>]*href="([^"]*bar/download/order\?id=[^"]+)"[^>]*>\s*<i></i>\s*CAJ下载', html)
                 or re.search(r'href="([^"]*bar/download/order\?id=[^"]+)"', html))
        if not m:
            raise RuntimeError("详情页未找到下载令牌（可能无全文权限）")
        bar_url = m.group(1)
        if bar_url.startswith("/"):
            bar_url = referer.split("/kcms2")[0] + bar_url
        # 走授权链（302 → docgateway → docdown），逐跳合并 Set-Cookie
        pdf_path, title = self._download_chain(bar_url, referer, out_dir)
        if verbose:
            print(f"OK  {title}\n    {pdf_path}")
        return pdf_path, title

    def _download_chain(self, bar_url, referer, out_dir):
        """走授权链（bar/download → docgateway → docdown）下载 PDF，curl_cffi 传输。"""
        os.makedirs(out_dir, exist_ok=True)
        try:
            from curl_cffi import requests as ccr
        except ImportError:
            raise RuntimeError("需要 curl_cffi：pip install curl_cffi")
        s = ccr.Session(impersonate="chrome", timeout=60)
        s.headers.update({"Accept-Language": "zh-CN,zh;q=0.9"})
        for k, v in self.cookies.items():
            s.cookies.set(k, v, domain="res.gxlib.org.cn")
        url, ref = bar_url, referer
        for _ in range(8):
            r = s.get(url, headers={"Referer": ref}, allow_redirects=False)
            if r.status_code == 302 and r.headers.get("location"):
                nxt = r.headers["location"]
                url = nxt if nxt.startswith("http") else urllib.parse.urljoin(url, nxt)
                ref = url.split("?")[0]
                continue
            if r.status_code == 200:
                cd = r.headers.get("content-disposition", "")
                fn_m = re.search(r"filename\*?=(?:utf-8''|UTF-8'')?\"?([^\";]+)", cd)
                fname = urllib.parse.unquote(fn_m.group(1)) if fn_m else "paper.pdf"
                body = r.content
                if body[:5] == b"%PDF-":
                    self.save_session()
                    out_path = os.path.join(out_dir, fname)
                    with open(out_path, "wb") as f:
                        f.write(body)
                    return out_path, fname.replace(".pdf", "")
                raise RuntimeError(f"授权链返回非PDF (HTTP {r.status_code}, {len(body)}B)")
            raise RuntimeError(f"授权链异常 HTTP {r.status_code} @ {url[:80]}")
        raise RuntimeError("授权链跳转超过 8 跳")

    def browser_trust(self, headless=True):
        """用 Playwright 建立信任态：登录平台 → 进知网 → 检索一次 → 导出全部 cookie（含 HttpOnly）。
        之后 search/download 即可纯 API 运行。依赖：pip install playwright && playwright install chromium。"""
        if not USERNAME or not PASSWORD:
            raise RuntimeError("未配置账号密码：请在同目录创建 config.json（可参考 config.example.json），"
                               "填入 username / password")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("需要 playwright：pip install playwright && python -m playwright install chromium")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
                locale="zh-CN", viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            # 1) 登录
            page.goto(PLATFORM + "/ermsLogin/view.do", timeout=60000)
            page.wait_for_timeout(1500)
            page.fill('input[name="userName"]', USERNAME)
            page.fill('input[name="password"]', PASSWORD)
            page.click('input[name="login"]')
            page.wait_for_timeout(4000)
            # 若仍在登录页：可能是 hasLogin，等待 reLogin 弹窗的「登录」链接出现并点击接管
            if "view.do" in page.url:
                try:
                    rl = page.locator('a[href="/ermsLogin/relogin.do"]')
                    rl.first.wait_for(state="visible", timeout=8000)
                    rl.first.click()
                    page.wait_for_timeout(4000)
                except Exception:
                    pass  # 弹窗未出现则可能已直接登录成功
            ok = page.evaluate("document.body.innerText.includes('您好') || document.body.innerText.includes('退出')")
            if not ok:
                browser.close()
                raise RuntimeError("平台登录失败（可能是验证码/账号锁定）")
            # 2) 进知网
            page.goto(PLATFORM + "/entry.do?rid=108&uid=361", timeout=60000)
            page.wait_for_timeout(4000)
            if "/verify/" in page.url:
                page.screenshot(path=os.path.join(HERE, "_trust_slider.png"))
                browser.close()
                raise RuntimeError("知网触发滑块验证（已截图 _trust_slider.png）："
                                   "请改用 headless=False 或人工在浏览器中通过一次")
            # 3) 检索一次建立完整信任态
            box = page.locator('input[name="txt_search"], input[placeholder*="中文文献"], textarea[name="txt_search"]').first
            if not box.count():
                box = page.locator('input[type="text"]').first
            try:
                box.fill("直播电商 消费者购买意愿", timeout=8000)
                page.get_by_text("检索", exact=True).first.click(timeout=8000)
                page.wait_for_timeout(4000)
            except Exception:
                pass  # 信任态核心 cookie 在进入知网时已建立，检索失败不阻断导出
            # 4) 导出全部 cookie（含 HttpOnly）
            cookies = ctx.cookies()
            self.cookies = {c["name"]: c["value"] for c in cookies}
            self.hosts = {}
            self.save_session()
            browser.close()
        print(f"信任态已建立：导出 {len(self.cookies)} 个 cookie（含 HttpOnly）")
        return True

    def cite(self, article_url, formats="GBTREFER,elearning,EndNote"):
        """获取 CNKI 原始引文（GB/T 7714-2025 / 知网研学 / EndNote）。
        返回 {format_key: text, ...}，另含 metadata（题名/作者/刊名/年/关键词/摘要）。"""
        page_html, referer = self.fetch_article_page(article_url)
        mu = re.search(r'id="export-url"[^>]*value="([^"]+)"', page_html)
        mi = re.search(r'id="export-id"[^>]*value="([^"]+)"', page_html)
        if not mu or not mi:
            raise RuntimeError("详情页未找到引文导出接口")
        export_url = html.unescape(mu.group(1))
        export_id = mi.group(1)
        try:
            from curl_cffi import requests as ccr
        except ImportError:
            raise RuntimeError("需要 curl_cffi")
        s = ccr.Session(impersonate="chrome", timeout=40)
        s.headers.update({"X-Requested-With": "XMLHttpRequest"})
        for k, v in self.cookies.items():
            s.cookies.set(k, v, domain="res.gxlib.org.cn")
        r = s.post(export_url, data={
            "filename": export_id,
            "displaymode": formats,
            "uniplatform": "NZKPT",
        }, headers={"Referer": article_url})
        try:
            d = json.loads(r.text)
        except json.JSONDecodeError:
            raise RuntimeError(f"引文接口响应异常: {r.text[:150]}")
        if d.get("code") != 1:
            raise RuntimeError(f"引文接口失败: {d.get('msg')}")
        out = {}
        for item in d.get("data", []):
            vals = [re.sub(r"<[^>]+>", "", v) for v in item.get("value", [])]
            out[item.get("key")] = vals
        # 附：从 E-Study 条目提取结构化元数据
        meta = {}
        for item in d.get("data", []):
            if "E-Study" in item.get("key", "") or "研学" in item.get("key", ""):
                for v in item.get("value", []):
                    for line in v.replace("<br>", "\n").split("\n"):
                        m = re.match(r"([^:：]{1,20})[：:](.+)", line.strip())
                        if m:
                            meta[m.group(1).strip()] = m.group(2).strip()
        if meta:
            out["_metadata"] = meta
        return out

    def import_cookies(self, cookie_str):
        """从浏览器会话导入 cookie（document.cookie 格式 'a=1; b=2'）。
        覆盖式导入：清空旧 cookie 与路由主机（会话已整体更换，残留旧会话 cookie 会毒化请求）。"""
        self.cookies = {}
        for part in cookie_str.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                self.cookies[k.strip()] = v.strip()
        self.hosts = {}
        self.save_session()
        print(f"已导入 {len(self.cookies)} 个 cookie（覆盖旧会话，路由主机待重新发现）")


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="广西图书馆·知网全文 API")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="程序化登录（无需浏览器）")
    p_trust = sub.add_parser("browser-trust", help="Playwright 建立信任态（登录+进知网+检索+导出含HttpOnly的cookie）")
    p_trust.add_argument("--headed", action="store_true", help="有头模式（滑块出现时可人工通过）")
    p_search = sub.add_parser("search", help="检索（支持来源类别/年度/排序筛选）")
    p_search.add_argument("keyword")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--core", default="", help="来源类别：北大核心/CSSCI/CSCD/AMI/EI/WJCI（逗号分隔可多选）")
    p_search.add_argument("--years", default="", help="发表年份范围，如 2020-2024")
    p_search.add_argument("--sort", default="被引", help="排序：被引(默认)/相关度/下载/综合/发表时间")
    p_search.add_argument("--order", default="desc", choices=["desc", "asc"])
    p_search.add_argument("--journals", default="", help="期刊白名单 CSV（一区/二区筛选，如 data/q1_q2_journals.example.csv，读第一列期刊名）")
    p_search.add_argument("--type", default="主题", help="检索字段：主题(默认)/篇关摘/关键词/篇名/全文/作者/第一作者/通讯作者/作者单位/基金/摘要/参考文献/分类号/文献来源")
    p_search.add_argument("--lang", default="中文", choices=["中文", "外文"], help="检索语言：中文(默认)/外文（英文文献）")
    p_exp = sub.add_parser("export", help="检索并批量导出（json/csv/ris/bibtex，可导入 Zotero/EndNote）")
    p_exp.add_argument("keyword")
    p_exp.add_argument("--type", default="主题")
    p_exp.add_argument("--lang", default="中文", choices=["中文", "外文"])
    p_exp.add_argument("--core", default="")
    p_exp.add_argument("--years", default="")
    p_exp.add_argument("--sort", default="被引")
    p_exp.add_argument("--limit", type=int, default=50)
    p_exp.add_argument("--fmt", default="json", choices=["json", "csv", "ris", "bibtex"])
    p_exp.add_argument("-o", "--out", default="", help="输出文件；缺省打印到屏幕")
    p_fm = sub.add_parser("find-match", help="按题名定位/验证某篇论文是否在库（字符匹配）")
    p_fm.add_argument("title")
    p_fm.add_argument("--limit", type=int, default=30)
    p_fc = sub.add_parser("format-citation", help="通用引文格式化（gb/t7714/apa/mla/chicago/vancouver）")
    p_fc.add_argument("--title", required=True)
    p_fc.add_argument("--authors", required=True, help="作者，; 或 , 分隔")
    p_fc.add_argument("--source", required=True, help="期刊/来源")
    p_fc.add_argument("--year", type=int, required=True)
    p_fc.add_argument("--volume", default="")
    p_fc.add_argument("--issue", default="")
    p_fc.add_argument("--pages", default="")
    p_fc.add_argument("--doi", default="")
    p_fc.add_argument("--style", default="gbt7714", choices=["gbt7714", "apa", "mla", "chicago", "vancouver"])
    p_dl = sub.add_parser("download", help="按文章详情页 URL 下载全文 PDF")
    p_dl.add_argument("urls", nargs="+")
    p_dl.add_argument("-o", "--out", default=DEFAULT_OUT)
    p_cite = sub.add_parser("cite", help="获取 CNKI 原始引文（GB/T 7714-2025 / 知网研学 / EndNote）")
    p_cite.add_argument("urls", nargs="+")
    p_cite.add_argument("--no-meta", action="store_true", help="不打印摘要关键词等元数据")
    p_meta = sub.add_parser("meta", help="提取详情页结构化元数据（摘要/关键词/基金/目录等）")
    p_meta.add_argument("urls", nargs="+")
    p_meta.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_imp = sub.add_parser("import-cookies", help="导入浏览器会话 cookie 后下载")
    p_imp.add_argument("cookie_str")
    sub.add_parser("session-info", help="查看当前会话")

    args = ap.parse_args()
    api = GxlibCNKI()

    if args.cmd == "login":
        api.login()
        api.ensure_cnki_hosts()
        print("登录成功。路由主机:", json.dumps(api.hosts, ensure_ascii=False))

    elif args.cmd == "browser-trust":
        api.browser_trust(headless=not args.headed)
        api.ensure_cnki_hosts()
        print("路由主机:", json.dumps(api.hosts, ensure_ascii=False))

    elif args.cmd == "search":
        try:
            yf = yt = None
            if args.years:
                parts = args.years.replace("～", "-").replace("~", "-").split("-")
                yf = int(parts[0].strip())
                yt = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
            papers = api.search(
                args.keyword, limit=args.limit,
                source_categories=args.core or None,
                year_from=yf, year_to=yt,
                sort_by=args.sort, sort_order=args.order,
                search_type=args.type, language=args.lang)
            if args.journals:
                import csv as _csv
                jf = args.journals if os.path.exists(args.journals) else os.path.join(HERE, args.journals)
                with open(jf, encoding="utf-8-sig") as f:
                    jlist = [row[0].strip() for row in _csv.reader(f) if row and row[0].strip()]
                before = len(papers)
                papers = api.filter_by_journals(papers, jlist)
                print(f"期刊白名单过滤: {before} → {len(papers)} 篇（名单 {len(jlist)} 刊）")
            total = getattr(api, "_last_total", "")
            print(f"检索到 {len(papers)} 篇（总结果 {total or '?'}）:")
            for i, p in enumerate(papers, 1):
                cited = f" 被引={p['cited']}" if p.get("cited") else ""
                print(f"  {i:>2}. {p['title'][:44] or p['url'][:60]} | {p.get('source','')[:12]} | {p.get('date','')[:10]}{cited}")
        except CaptchaError as e:
            print("\n!! 检索被知网滑块验证拦截（反爬）。")
            print("   处理方式：请在弹出的浏览器中手动拖动滑块一次，之后本会话自动解锁。")
            print("   滑块地址：", e.verify_url)
            print("   也可以手动打开上面的地址完成验证。")
            sys.exit(2)

    elif args.cmd == "export":
        try:
            yf = yt = None
            if args.years:
                parts = args.years.replace("～", "-").replace("~", "-").split("-")
                yf = int(parts[0].strip())
                yt = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
            papers = api.search(args.keyword, limit=args.limit,
                                source_categories=args.core or None,
                                year_from=yf, year_to=yt, sort_by=args.sort,
                                search_type=args.type, language=args.lang)
            out = api.export_papers(papers, args.fmt)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(out)
                print(f"已导出 {len(papers)} 篇 → {args.out}")
            else:
                sys.stdout.write(out + "\n")
        except CaptchaError as e:
            print("检索被滑块拦截：", e.verify_url[:120]); sys.exit(2)
        except Exception as e:
            print(f"FAIL: {e}")

    elif args.cmd == "find-match":
        try:
            matches = api.find_best_match(args.title, limit=args.limit)
            print(f"题名匹配（共比较 {len(matches)} 条候选）：")
            for m in matches[:5]:
                print(f"  {m['ratio']:.2f}  {m['title'][:40]} | {m.get('source','')[:12]} | {m.get('date','')[:10]} | {m['url'][:60]}")
        except Exception as e:
            print(f"FAIL: {e}")

    elif args.cmd == "format-citation":
        print(api.format_citation(args.title, args.authors, args.source, args.year,
                                  args.volume, args.issue, args.pages, args.doi, args.style))

    elif args.cmd == "download":
        for u in args.urls:
            try:
                api.download(u, out_dir=args.out)
            except Exception as e:
                print(f"FAIL {u[:70]}: {e}")

    elif args.cmd == "cite":
        for u in args.urls:
            try:
                cites = api.cite(u)
                for key in cites:
                    if key == "_metadata" and args.no_meta:
                        continue
                    print(f"### {key}")
                    if key == "_metadata":
                        for k, v in cites[key].items():
                            print(f"  {k}: {v[:120]}")
                    else:
                        for line in cites[key]:
                            print("  ", line[:300])
            except Exception as e:
                print(f"FAIL {u[:70]}: {e}")

    elif args.cmd == "meta":
        for u in args.urls:
            try:
                meta = api.meta(u)
                if args.json:
                    print(json.dumps(meta, ensure_ascii=False, indent=1))
                else:
                    for k, v in meta.items():
                        if isinstance(v, list):
                            print(f"{k}: {len(v)} 项")
                            for x in v[:12]:
                                print(f"  - {x[:60]}")
                        else:
                            print(f"{k}: {v[:200]}")
            except Exception as e:
                print(f"FAIL {u[:70]}: {e}")

    elif args.cmd == "import-cookies":
        api.import_cookies(args.cookie_str)

    elif args.cmd == "session-info":
        print("cookies:", json.dumps(api.cookies, ensure_ascii=False))
        print("hosts:", json.dumps(api.hosts, ensure_ascii=False))


if __name__ == "__main__":
    main()
