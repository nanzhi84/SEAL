"""Declarative target plan for the benchmark (bounded, auditable).

`follow_next_max` bounds pagination so a list recipe cannot run away.
Every target maps to one recipe in bench/recipes.py.
"""
from __future__ import annotations

TARGETS: list[dict] = [
    # ---- Test 1 + Test 2: A 最高人民法院指导案例 (HTML list/pagination/detail)
    {"id": "A_list_p1", "test": "T2", "kind": "list", "recipe": "court_list",
     "url": "https://www.court.gov.cn/shenpan/gengduo/77.html", "follow_next_max": 1},
    {"id": "A_detail_1", "test": "T2", "kind": "detail", "recipe": "court_detail",
     "url": "https://www.court.gov.cn/shenpan/xiangqing/490521.html", "parent": "A_list_p1"},
    {"id": "A_detail_2", "test": "T2", "kind": "detail", "recipe": "court_detail",
     "url": "https://www.court.gov.cn/shenpan/xiangqing/490511.html", "parent": "A_list_p1"},
    {"id": "A_detail_3", "test": "T2", "kind": "detail", "recipe": "court_detail",
     "url": "https://www.court.gov.cn/shenpan/xiangqing/490501.html", "parent": "A_list_p1"},
    {"id": "A_detail_4", "test": "T2", "kind": "detail", "recipe": "court_detail",
     "url": "https://www.court.gov.cn/shenpan/xiangqing/490491.html", "parent": "A_list_p1"},
    {"id": "A_detail_5", "test": "T2", "kind": "detail", "recipe": "court_detail",
     "url": "https://www.court.gov.cn/shenpan/xiangqing/490481.html", "parent": "A_list_p1"},

    # ---- Test 1: B 中国政府采购网 (announcement discovery + body extraction)
    {"id": "B_home", "test": "T1", "kind": "list", "recipe": "ccgp_list",
     "url": "https://www.ccgp.gov.cn/"},
    {"id": "B_detail_1", "test": "T1", "kind": "detail", "recipe": "ccgp_detail",
     "url": "https://www.ccgp.gov.cn/news/202609/t20260928_27408954.htm", "parent": "B_home"},

    # ---- Test 3: C 国家新闻出版署 PDF
    {"id": "C_pdf", "test": "T3", "kind": "pdf", "recipe": None,
     "url": "https://www.nppa.gov.cn/bsfw/cyjghcpcx/202112/P020211208753495011054.pdf"},

    # ---- Test 1: D UK Companies House (structured fields)
    {"id": "D_home", "test": "T1", "kind": "list", "recipe": "ch_search",
     "url": "https://find-and-update.company-information.service.gov.uk/"},
    {"id": "D_company", "test": "T1", "kind": "detail", "recipe": "ch_company",
     "url": "https://find-and-update.company-information.service.gov.uk/company/00445790",
     "parent": "D_home"},

    # ---- Test 4: dynamic / iframe / restricted
    {"id": "E_list", "test": "T4", "kind": "special", "recipe": "stats_iframe",
     "url": "https://www.stats.gov.cn/xxgk/list4.html"},
    {"id": "F_home", "test": "T4", "kind": "special", "recipe": "stats_page",
     "url": "https://www.creditchina.gov.cn/"},
    {"id": "G_home", "test": "T4", "kind": "special", "recipe": "stats_page",
     "url": "https://www.nmpa.gov.cn/datasearch/home-index.html"},

    # ---- Test 2 (secondary): H 最高检指导案例
    {"id": "H_list", "test": "T2", "kind": "list", "recipe": "spp_list",
     "url": "https://www.spp.gov.cn/spp/jczdal/index.shtml", "follow_next_max": 0},
    {"id": "H_detail_1", "test": "T2", "kind": "detail", "recipe": "spp_detail",
     "url": "https://www.spp.gov.cn/spp/jczdal/202608/t20260805_733868.shtml", "parent": "H_list"},
]

# Test 4 third group: which targets get a Playwright re-attempt.
# E_list is a static shell whose iframes the recipe already enumerates, so the
# browser run focuses on the two genuinely JS-dependent/restricted sources.
PLAYWRIGHT_TARGETS = []  # F/G returned 412: do not retry access restrictions.

# Firecrawl plan (credits counted per scraped page).
FIRECRAWL_SCRAPE = ["A_detail_1", "B_detail_1", "D_company", "E_list", "C_pdf"]
FIRECRAWL_CRAWL = {"id": "A_crawl", "url": "https://www.court.gov.cn/shenpan/gengduo/77.html",
                   "limit": 3, "max_discovery_depth": 1}
FIRECRAWL_PDF = "C_pdf"

BY_ID = {t["id"]: t for t in TARGETS}