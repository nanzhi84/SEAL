"""Public template contracts authored independently from adapters and parsers."""

import json
from urllib.parse import parse_qs

API = "/portal/front/financial/fundTrustee/findFundTrusteesPage"
MUTUAL = "/portal/front/mutualFund/findMutualFundHousePage"
PERMIT = "/perxxgkinfo/syssb/xkgg/xkgg!licenseInformation.action"


def html(value):
    return "text/html; charset=utf-8", ("<html>" + value + "</html>").encode()


def resources():
    data = {
        "/adapter/list": html(
            '<h1>List</h1><div class="documents"><a href="/adapter/detail">First</a></div><a class="next" href="/adapter/page2">Next</a>'
        ),
        "/adapter/page2": html(
            '<h1>Page two</h1><div class="documents"><a href="/adapter/detail2">Second</a></div>'
        ),
        "/adapter/detail": html(
            '<article>First full document</article><iframe src="/adapter/frame"></iframe><a class="attachment" href="/documents/report.pdf">PDF</a>'
        ),
        "/adapter/detail2": html("<article>Second full document</article>"),
        "/adapter/frame": html("<article>Frame full document</article>"),
        "/shenpan/gengduo/77.html": html(
            '<h1>Court list</h1><div class="sec_list"><a href="/shenpan/xiangqing/one.html">First</a></div><div class="page"><a href="/shenpan/gengduo/77_2.html">下一页</a><a href="/shenpan/gengduo/77_2.html">尾页</a></div>'
        ),
        "/shenpan/gengduo/77_2.html": html(
            '<h1>Court list two</h1><div class="sec_list"><a href="/shenpan/xiangqing/two.html">Second</a></div><div class="page"><a href="/shenpan/gengduo/77_2.html">尾页</a></div>'
        ),
        "/shenpan/xiangqing/one.html": html(
            '<div class="detail"><div class="title">Court first</div><div class="txt_txt">Public first text<a href="/documents/report.pdf">PDF</a></div></div>'
        ),
        "/shenpan/xiangqing/two.html": html(
            '<div class="detail"><div class="title">Court second</div><div class="txt_txt">Public second text</div></div>'
        ),
        "/spp/jczdal/index.shtml": html(
            "<h1>Prosecutor list</h1><div class=\"commonList_con\"><div class=\"li_line\"><a href=\"/spp/detail1\">First</a></div></div><script>createPageHTML('page_div',2,1,'index','shtml',2)</script>"
        ),
        "/spp/jczdal/index_2.shtml": html(
            "<h1>Prosecutor list two</h1><div class=\"commonList_con\"><div class=\"li_line\"><a href=\"/spp/detail2\">Second</a></div></div><script>createPageHTML('page_div',2,2,'index','shtml',2)</script>"
        ),
        "/spp/detail1": html('<div id="fontzoom">Prosecutor first text</div>'),
        "/spp/detail2": html('<div id="fontzoom">Prosecutor second text</div>'),
        "/education/notice": html(
            '<h1>College notice</h1><div class="TRS_Editor">Public workbook<a href="/documents/workbook.xls">XLS</a></div>'
        ),
        "/beijing/xzcfxxgs/index.html": html(
            '<h1>Penalties</h1><iframe src="/www/illegal?siteid=beijing"></iframe>'
        ),
        "/www/illegal/decision1": html("<h1>Penalty first</h1>Full public decision one"),
        "/www/illegal/decision2": html("<h1>Penalty second</h1>Full public decision two"),
        "/legal/detail": html(
            '<h1>Official legal document</h1><p>Full legal text</p><a href="/documents/report.pdf">PDF</a>'
        ),
        "/permit/detail1": html(
            '<div class="tab0">Permit first</div><table id="apply_table"><tr><td>Full public permit one</td></tr></table>'
        ),
        "/permit/detail2": html(
            '<div class="tab0">Permit second</div><table id="apply_table"><tr><td>Full public permit two</td></tr></table>'
        ),
    }
    for source, api in [("trustee", API), ("mutual", MUTUAL)]:
        data["/amac/" + source] = html(
            f'<h1>Directory</h1><script>url: URL + "{api}", pageNo: 1, pageSize: 10</script>'
        )
    return data


def response(path, query, nonce, form=None):
    if path in (API, MUTUAL):
        page = int(parse_qs(query).get("pageNo", ["1"])[0])
        rows = [
            {"public_name": f"Entity {i}", "nested": {"public": True}}
            for i in range((page - 1) * 10, min(page * 10, 11))
        ]
        value = {"code": 200, "data": {"errcode": 0, "data": {"total": 11, "dataList": rows}}}
        return "application/json", json.dumps(value).encode()
    if path in ("/www/illegal", "/www/illegal/index"):
        page = int(parse_qs(query).get("page", ["1"])[0])
        return html(
            f'<h1>Penalty page {page}</h1><span id="page">{page}</span><span id="total">2</span><div class="cfxx_jg"><table><tr><td><a href="/www/illegal/decision{page}">Decision {page}</a></td></tr></table></div><a href="/www/illegal/index?siteid=beijing">Next</a>'
        )
    if path == "/legal/search":
        value = f'<h1>Legal search</h1><form id="form1" method="post" action="/legal/search"><input type="hidden" name="__VIEWSTATE" value="public-state-{nonce}"><input type="hidden" name="__EVENTVALIDATION" value="public-validation-{nonce}"><select name="ctrl_191017_163$drdDocCategory"><option value="1">Category one</option></select><select name="ctrl_191017_163$drdDocYear"><option value="2026">2026</option></select><input type="submit" name="ctrl_191017_163$btnSearch" value="Search"></form>'
        if form is not None:
            value += '<a href="/legal/detail?docid=1">Legal result</a>'
        return html(value)
    if path == PERMIT:
        page = int((form or {}).get("page.pageNo", ["1"])[0])
        fields = [
            "page.pageNo",
            "page.orderBy",
            "page.order",
            "tempReportKey",
            "province",
            "city",
            "management",
            "registerentername",
            "xkznum",
            "treadname",
            "treadcode",
            "publishtime",
        ]
        values = {"page.pageNo": str(page), "tempReportKey": f"{nonce:032x}"}
        inputs = "".join(
            f'<input name="{key}" id="{"pageNo" if key == "page.pageNo" else key}" value="{values.get(key, "")}">'
            for key in fields
        )
        cells = (
            f'<td><a href="/permit/detail{page}?xkgk=getxxgkContent">Permit {page}</a></td>'
            + "<td>Public value</td>" * 8
        )
        return html(
            f'<h1>Permit list</h1><form id="mainForm" method="post" action="{PERMIT}">{inputs}</form><p>共 2 页</p><table class="tabtd"><tr class="tbhead"><th>Header</th></tr><tr>{cells}</tr></table>'
        )
    return None
