import json
from datetime import timedelta

import pytest

import news_sources as ns
from test_watch import CFG, watch
from test_verification import Response, Session, product

SKU = "https://item.jd.com/123.html"
API = "https://p.3.cn/prices/mgets?skuIds=J_123"
TITLE = "华硕 RTX 5060 Ti 8G 显卡"


def collector(responses=None, **settings):
    return ns.Collector(Session(responses or {}), {"max_requests": 10, "budget_seconds": 30, **settings})


def post(text="RTX 5060 Ti 8G 券后2899元", age=0, links=None, title=TITLE):
    stamp = (watch.now_cn() - timedelta(minutes=age)).isoformat() if age is not None else ""
    return ns.Lead("小红书", title, text, "https://www.xiaohongshu.com/explore/abc", stamp,
                   [SKU] if links is None else links)


def test_bilibili_preserves_real_fields():
    data = {"code": 0, "data": {"result": [{"bvid": "BV123", "title": "<em>5060 Ti</em> 8G",
            "description": "券后2899元 " + SKU, "pubdate": 1791558000}]}}
    lead = ns.parse_bilibili(data)[0]
    assert lead.title == "5060 Ti 8G" and lead.url.endswith("BV123")
    assert lead.published_at.endswith("+00:00") and lead.product_urls == [SKU]
    with pytest.raises(ValueError):
        ns.parse_bilibili({"code": -412})


def test_manmanbuy_card_does_not_inherit_other_cards():
    page = '<div><div class="DiscountItemPC_itemInfo_abc"><div class="itemTitle_x"><a href="/discuxiao_123.aspx">索泰 RTX 5060 Ti 8G</a></div><p>3899.7元（含国补）</p></div></div><div>RTX 5060 8G 1999元</div>'
    lead = ns.parse_comparison(page, "https://cu.manmanbuy.com/", "manmanbuy")[0]
    assert "3899.7" in lead.text and "1999" not in lead.text
    assert lead.url == "https://cu.manmanbuy.com/discuxiao_123.aspx" and not lead.published_at


def test_xhs_state_is_parsed_without_executing_javascript():
    page = '<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"a":{"note":{"title":"5060 Ti 8G","desc":"undefined ' + SKU + '","time":1791558000000}}}},"unused":undefined};alert(1)</script>'
    leads = ns.parse_xiaohongshu(page, "https://www.xiaohongshu.com/explore/a")
    assert len(leads) == 1 and "undefined" in leads[0].text
    assert leads[0].product_urls == [SKU]
    assert ns.parse_xiaohongshu('<script>window.__INITIAL_STATE__={"search":{"feeds":[]}}</script>', "https://www.xiaohongshu.com/search_result") == []


def test_weibo_json_links_and_publication():
    page = json.dumps({"data": {"idstr": "123", "text": '<p>RTX 5060 Ti 8G <a href="' + SKU + '">2899元</a></p>',
                               "created_at": "Fri Oct 09 12:00:00 +0800 2026"}})
    lead = ns.parse_weibo(page, "https://m.weibo.cn/statuses/show?id=123")[0]
    assert lead.product_urls == [SKU] and lead.published_at == "2026-10-09T12:00:00+08:00"


@pytest.mark.parametrize("stamp,valid", [("2026-10-09T12:00:00Z", True), (1791558000, True),
    ("Fri, 09 Oct 2026 12:00:00 +0800", True), ("2026-10-09 12:00", False), ("刚刚", False), (None, False)])
def test_publication_requires_reliable_time(stamp, valid):
    assert bool(ns.published(stamp)) == valid


def test_rss_and_atom_choose_article_link_and_preserve_product_link():
    rss = '<rss><channel><item><title>5060 Ti 8G</title><link>https://example.com/p/1</link><description><![CDATA[<a href="' + SKU + '">券后2899元</a>]]></description><pubDate>Fri, 09 Oct 2026 12:00:00 +0800</pubDate></item></channel></rss>'
    lead = ns.parse_feed(rss, "https://example.com/feed", "RSS测试")[0]
    assert lead.product_urls == [SKU] and lead.published_at
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>5060 Ti</title><link rel="alternate" href="https://example.com/article"/><link rel="self" href="https://example.com/feed/1"/><updated>2026-10-09T12:00:00Z</updated></entry></feed>'
    assert ns.parse_feed(atom, "https://example.com/feed")[0].url.endswith("/article")


@pytest.mark.parametrize("page", ["<html><body>2899元</body></html>", '<!DOCTYPE rss [<!ENTITY foo "bar">]><rss/>', "not XML"])
def test_feed_rejects_html_and_entities(page):
    with pytest.raises(ValueError):
        ns.parse_feed(page, "https://example.com/feed")


@pytest.mark.parametrize("age", [None, 241, -6])
def test_missing_stale_future_posts_cannot_alert(age):
    scan = collector({SKU: Response(product())})
    candidates = watch.additional_post_candidates(post(age=age), scan, CFG)
    assert candidates and all(not watch.should_alert(c, CFG) for c in candidates)
    assert scan.calls == 0


@pytest.mark.parametrize("text", ["RTX 5060 8G 券后2899元", "RTX 5060 Ti 16G 券后2899元",
    "最低2899元起", "RTX 5060 Ti 8G / RTX 5060 8G 券后2899元", "RTX 5060 Ti 8G 游戏整机2899元"])
def test_title_never_proves_wrong_or_floor_price(text):
    candidates = watch.additional_post_candidates(post(text), collector({SKU: Response(product())}), CFG)
    assert all(not c.price_verified and not watch.should_alert(c, CFG) for c in candidates)


@pytest.mark.parametrize("links", [[], [SKU, "https://item.jd.com/456.html"]])
def test_post_needs_one_bound_purchase_link(links):
    scan = collector({SKU: Response(product())})
    candidates = watch.additional_post_candidates(post(links=links), scan, CFG)
    assert candidates and all(not c.price_verified for c in candidates) and scan.calls == 0


def test_fresh_bound_post_and_exact_sku_can_alert_and_dedupe():
    candidates = watch.additional_post_candidates(post(), collector({SKU: Response(product())}), CFG)
    candidate = candidates[0]
    assert candidate.price_verified and candidate.price == 2899 and watch.should_alert(candidate, CFG)
    assert "原帖" in watch.format_message(candidate, CFG)
    current = watch.current_sku_candidate(SKU, collector({SKU: Response(product())}), CFG)
    assert watch.alert_key(candidate) == watch.alert_key(current)
    state = {"alerts": {watch.alert_key(current): {"price": 2899, "time": watch.now_cn().isoformat()}}}
    assert not watch.dedupe_ok(candidate, state, CFG)


@pytest.mark.parametrize("page", [product(name="华硕 RTX 5060 8G"), product(name="华硕 RTX 5060 Ti 16G"),
    product(offer_type="AggregateOffer"), product(sku="456"), '<h1>RTX 5060 Ti 8G</h1>'])
def test_purchase_mismatch_or_missing_selected_sku_blocks_post(page):
    candidates = watch.additional_post_candidates(post(), collector({SKU: Response(page)}), CFG)
    assert candidates and all(not c.price_verified for c in candidates)


@pytest.mark.parametrize("payload", [[{"id": "J_456", "p": "2899"}], [{"id": "J_123", "p": "-1"}], {}])
def test_jd_live_api_must_match_sku_and_have_valid_price(payload):
    with pytest.raises(ValueError):
        watch.current_sku_candidate(SKU, collector({SKU: Response('<div class="sku-name">' + TITLE + '</div>'), API: Response(json.dumps(payload))}), CFG)


def test_jd_current_price_uses_exact_sku():
    scan = collector({SKU: Response('<div class="sku-name">' + TITLE + '</div>'), API: Response('[{"id":"J_123","p":"2899"}]')})
    candidate = watch.current_sku_candidate(SKU, scan, CFG)
    assert candidate.price == 2899 and candidate.price_verified and watch.should_alert(candidate, CFG)


@pytest.mark.parametrize("url", ["https://localhost/feed", "https://127.0.0.1/feed", "https://10.0.0.1/feed", "http://example.com/feed", "https://user:pass@example.com/feed"])
def test_feed_url_rejects_non_public_endpoints(url):
    assert not ns.public_feed_url(url)


def test_collector_deadline_cap_redirect_guard_and_close():
    scan = collector({SKU: Response(product())}, max_requests=1)
    scan.fetch(SKU, "sku")
    scan.fetch(SKU, "sku")  # cached requests consume no additional budget
    with pytest.raises(TimeoutError):
        scan.fetch(API, "sku")
    assert scan.calls == 1 and scan.session.responses[SKU].closed
    redirect = Response(status=302, location="https://example.com/page")
    scan = collector({SKU: redirect})
    with pytest.raises(ValueError):
        scan.fetch(SKU, "sku")
    assert redirect.closed and len(scan.session.calls) == 1
    scan = ns.Collector(Session({}), {"budget_seconds": 0})
    with pytest.raises(TimeoutError):
        scan.fetch(SKU, "sku")


def test_failure_isolation_and_honest_missing_source_status():
    cfg = {name: {"enabled": True, "urls": []} for name in ("xiaohongshu", "weibo", "gwdang")}
    cfg.update({"manmanbuy": {"enabled": False}, "bilibili": {"enabled": False},
                "rss": [{"url": "https://example.com/bad"}, {"url": "https://example.com/good"}]})
    scan = collector({"https://example.com/bad": Response("<html/>"),
                      "https://example.com/good": Response('<rss><channel><item><title>5060 Ti 8G</title><link>https://example.com/post</link></item></channel></rss>')}, **cfg)
    assert len(scan.collect()) == 1
    assert [s["status"] for s in scan.status] == ["needs_input", "needs_input", "needs_input", "unavailable", "ok"]


def test_sku_history_discovery_and_source_report(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "SOURCE_REPORT_PATH", tmp_path / "source_scan.json")
    monkeypatch.setattr(watch, "_read_history_rows", lambda: [{"source": "商品SKU监控", "title": TITLE, "url": SKU}])
    settings = {p: {"enabled": False} for p in ("manmanbuy", "bilibili", "xiaohongshu", "weibo", "gwdang")}
    settings.update({"enabled": True, "sku_watch": {"enabled": True, "discover_from_history": True}})
    candidates = watch.fetch_additional_sources([], {**CFG, "additional_sources": settings}, Session({SKU: Response(product())}))
    report = json.loads((tmp_path / "source_scan.json").read_text(encoding="utf-8"))
    assert len(candidates) == 1 and report["sources"][0]["status"] == "ok" and report["requests"] == 1


def test_dashboard_shows_source_status_and_escapes_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "SOURCE_REPORT_PATH", tmp_path / "source_scan.json")
    monkeypatch.setattr(watch, "DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(watch, "INDEX_PATH", tmp_path / "docs/index.html")
    watch.SOURCE_REPORT_PATH.write_text(json.dumps({"sources": [{"source": "小红书", "status": "needs_input",
        "items": 0, "reason": "<script>bad()</script>"}]}), encoding="utf-8")
    watch.write_dashboard([])
    page = watch.INDEX_PATH.read_text(encoding="utf-8")
    assert "待添加链接" in page and "&lt;script&gt;bad()&lt;/script&gt;" in page
    assert "<script>bad()</script>" not in page
