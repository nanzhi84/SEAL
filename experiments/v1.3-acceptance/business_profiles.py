"""Reviewed public Resource contracts, retaining original business-map seeds."""

from live import SEEDS

ABSENT = {
    "dd-017": {
        "url": "https://www.court.gov.cn/robots.txt",
        "body_sha256": "ee40e4c1bdab81e044b67f18e71d1bd613354b5f2b3d631020c907cffc4adbcb",
    },
    "dd-041": {
        "url": "https://www.spp.gov.cn/",
        "body_sha256": "6c43c58ec1eb4156d624d358bee66747d1e96e3b767c4fd26d1c718cf5da24d8",
    },
}
PATHS = {
    "dd-017": ["/shenpan"],
    "dd-041": ["/spp/jczdal", "/spp/xhtml/js/page.js", "/xwfbh/wsfbt", "/spp/xwfbh/wsfbh"],
    "dd-250": ["/fwdt/wyc/jgcprycx/jgcx/jjtgrjjgs", "/portal/front/financial/fundTrustee"],
    "dd-247": ["/fwdt/wyc/jgcprycx/jgcx/gmjjglrml", "/portal/front/mutualFund"],
    "dd-468": ["/jyb_xxgk/s5743/s5744/A03/202110"],
    "dd-102": ["/beijing/xzcfxxgs", "/www/illegal"],
    "dd-357": ["/permitExt/defaults", "/perxxgkinfo"],
}


def profile(identity):
    seed = SEEDS[identity]
    host = seed.split("/")[2]
    requests = 1200 if identity in {"dd-017", "dd-041"} else 200
    config = {
        "id": "business-" + identity,
        "research_ids": [identity],
        "entry_urls": [seed],
        "allowed_hosts": [host],
        "allowed_path_prefixes": PATHS[identity],
        "host_path_scopes": {host: PATHS[identity]},
        "methods": ["GET", "HEAD"],
        "identity": "business_key",
        "output_schema": "record.v1",
        "archive_approved": True,
        "scope": (
            "Entire publicly enumerated business Resource reached from the original seed; "
            "all declared pagination, business details and business attachments. "
            "No homepage navigation, guessed search or fixed sample limit. "
            "Execution budget is not a coverage claim. Nationwide permit enumeration "
            "requires verified public partitions and explicit remaining coverage."
        ),
        "seed_role": "list",
        "budget": {"requests": requests, "seconds": 1800, "response_bytes": 20 * 1024 * 1024},
        "concurrency": 2,
        "delay": 0.2,
        "robots": True,
        "robots_redirect_paths": ["/"] if identity == "dd-041" else [],
        "robots_absent_responses": [ABSENT[identity]] if identity in ABSENT else [],
        "user_agent": "SEAL/1.3 (+public business Resource verification)",
    }

    if identity == "dd-357":
        origin = "https://permit.mee.gov.cn"
        config["methods"].append("POST")
        config["budget"] = {"requests": 10000, "seconds": 3600, "response_bytes": 20 * 1024 * 1024}
        config["public_post_rules"] = [
            {
                "url": origin + "/perxxgkinfo/syssb/xkgg/xkgg!getRegions.action",
                "fields": {"parentCode": "region"},
            },
            {
                "url": origin + "/perxxgkinfo/syssb/xkgg/xkgg!licenseInformation.action",
                "fields": {
                    "page.pageNo": "page",
                    "page.orderBy": "empty",
                    "page.order": "empty",
                    "tempReportKey": "query_id",
                    "province": "region",
                    "city": "region",
                    "management": "management",
                    "registerentername": "empty",
                    "xkznum": "empty",
                    "treadname": "empty",
                    "treadcode": "empty",
                    "publishtime": "empty",
                },
            },
        ]
    return config
