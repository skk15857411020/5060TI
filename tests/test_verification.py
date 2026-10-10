import json
from datetime import timedelta
from pathlib import Path

import pytest

from test_watch import CFG, watch


TITLE = "华硕 RTX 5060 Ti 8G 显卡"
URL = "https://www.smzdm.com/p/123456/"


def article(body, purchase=""):
    link = f'<a class="J_buy" href="{purchase}">购买</a>' if purchase else ""
    return f'<h1>{TITLE}</h1><div class="article-content">{body}</div>{link}'


def product(name="华硕 RTX 5060 Ti 8G", sku="123", offer_type="Offer"):
    value = {"@type": "Product", "name": name, "sku": sku,
             "offers": {"@type": offer_type, "price": "3299", "priceCurrency": "CNY"}}
    return '<script type="application/ld+json">' + json.dumps(value) + '</script>'


class Response:
    def __init__(self, page="", status=200, location=""):
        self.page = page
        self.status_code = status
        self.headers = {"Location": location}
        self.encoding = "utf-8"
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def iter_content(self, size):
        yield self.page.encode("utf-8")

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def lead(**kwargs):
    return watch.Candidate("什么值得买API", "京东", TITLE, 2899, URL,
                           price_verified=False, **kwargs)


def test_detail_recovers_a_low_confidence_lead_and_saves_exact_evidence():
    candidate = lead()
    session = Session({URL: Response(article('<p>RTX 5060 Ti <span>8GB</span> 国补后2899元</p>'))})
    watch.recheck_smzdm_candidates([candidate], CFG, session)
    assert candidate.price_verified
    assert candidate.price == 2899
    assert "8GB" in candidate.price_check and "2899" in candidate.price_check
    assert candidate.evidence_url == URL
    assert watch.should_alert(candidate, CFG)
    assert "核验依据" in watch.format_message(candidate, CFG)


def test_detail_replaces_5060_low_price_with_ti_price():
    candidate = lead()
    session = Session({URL: Response(article(
        '<p>RTX 5060 8G 券后2899元</p><p>RTX 5060 Ti 8G 券后3299元</p>'
    ))})
    watch.recheck_smzdm_candidates([candidate], CFG, session)
    assert candidate.price_verified and candidate.price == 3299
    assert not watch.should_alert(candidate, CFG)
    assert "3299" in candidate.price_check and "2899" not in candidate.price_check


def test_recommendations_do_not_verify_the_article():
    candidate = lead()
    page = article('<p>RTX 5060 8G 券后2899元</p>') + '<div>推荐：RTX 5060 Ti 8G 券后2799元</div>'
    watch.recheck_smzdm_candidates([candidate], CFG, Session({URL: Response(page)}))
    assert not candidate.price_verified
    assert not watch.should_alert(candidate, CFG)


def test_purchase_link_pointing_to_5060_blocks_alert():
    candidate = lead()
    buy = "https://item.jd.com/123.html"
    session = Session({URL: Response(article('<p>RTX 5060 Ti 8G 到手2899元</p>', buy)),
                       buy: Response(product(name="华硕 RTX 5060 8G"))})
    watch.recheck_smzdm_candidates([candidate], CFG, session)
    assert not candidate.price_verified
    assert "规格不符" in candidate.price_check


def test_exact_jd_sku_allows_coupon_price_with_higher_base_price():
    candidate = lead()
    buy = "https://item.jd.com/123.html"
    session = Session({URL: Response(article('<p>RTX 5060 Ti 8G 国补后2899元</p>', buy)),
                       buy: Response(product())})
    watch.recheck_smzdm_candidates([candidate], CFG, session)
    assert candidate.price_verified
    assert candidate.price == 2899
    assert "SKU=123" in candidate.purchase_check


@pytest.mark.parametrize("page", [product(offer_type="AggregateOffer"), product(sku="456")])
def test_aggregate_offer_or_unselected_sku_is_not_confirmation(page):
    status, _ = watch.verify_purchase_sku(page, "https://item.jd.com/123.html", TITLE, CFG)
    assert status == "unknown"


def test_taobao_parent_product_does_not_identify_selected_sku():
    status, _ = watch.verify_purchase_sku(product(), "https://item.taobao.com/item.htm?id=987", TITLE, CFG)
    assert status == "unknown"
    status, _ = watch.verify_purchase_sku(product(), "https://item.taobao.com/item.htm?id=987&skuId=123", TITLE, CFG)
    assert status == "matched"


def test_fetch_failure_revokes_search_summary_confirmation():
    candidate = lead()
    candidate.price_verified = True
    watch.recheck_smzdm_candidates([candidate], CFG, Session({URL: TimeoutError()}))
    assert not candidate.price_verified
    assert "失败" in candidate.price_check


def test_budget_and_cache_bound_extra_requests():
    candidates = [lead(), lead(), watch.Candidate("什么值得买API", "京东", TITLE, 2899, URL + "2")]
    session = Session({URL: Response(article('<p>RTX 5060 Ti 8G 到手2899元</p>'))})
    watch.recheck_smzdm_candidates(candidates, {**CFG, "detail_verify_max_articles": 1}, session)
    assert len(session.calls) == 1
    assert candidates[0].price_verified and candidates[1].price_verified
    assert not candidates[2].price_verified
    assert "预算" in candidates[2].price_check


def test_zero_budget_and_stale_or_expensive_posts_do_not_fetch():
    fresh = lead()
    old = lead(published_at=(watch.now_cn() - timedelta(days=2)).isoformat())
    expensive = lead()
    expensive.price = 3999
    session = Session({})
    watch.recheck_smzdm_candidates([fresh, old, expensive], {**CFG, "detail_verify_budget_seconds": 0}, session)
    assert session.calls == []
    assert not fresh.price_verified


def test_redirect_cannot_fetch_an_unsupported_host():
    response = Response(status=302, location="https://127.0.0.1/private")
    session = Session({URL: response})
    with pytest.raises(ValueError):
        watch._fetch_verification_page(session, URL, watch.time.monotonic() + 5, article_only=True)
    assert len(session.calls) == 1
    assert response.closed


def test_history_and_report_keep_evidence_and_exclude_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "HISTORY_PATH", tmp_path / "history.csv")
    monkeypatch.setattr(watch, "VERIFICATION_PATH", tmp_path / "checks.json")
    candidate = lead()
    candidate.price_verified = True
    candidate.price_check = "详情页 RTX 5060 Ti 8G 券后2899元"
    candidate.evidence_url = URL
    unverified = lead()
    unverified.price = 1999
    rows = watch.record_price_history([candidate, unverified])
    assert len(rows) == 1 and float(rows[0]["price"]) == 2899
    assert rows[0]["price_evidence"] == candidate.price_check
    watch.write_verification_report([candidate, unverified])
    report = json.loads((tmp_path / "checks.json").read_text(encoding="utf-8"))
    assert [item["verified"] for item in report["candidates"]] == [True, False]


def test_payable_3099_is_not_mistaken_for_a_gpu_model():
    result = watch.verify_smzdm_offer({"article_title": TITLE,
                                     "article_content": "RTX 5060 Ti 8G 国补后3099元"}, CFG)
    assert result[4] and result[0] == 3099


def test_price_with_thousands_separator_is_bound_correctly():
    result = watch.verify_smzdm_offer({"article_title": TITLE,
                                     "article_content": "RTX 5060 Ti 8G 券后¥2,899"}, CFG)
    assert result[4] and result[0] == 2899


def test_real_smzdm_selected_5060_overrides_ti_headline():
    page = (Path(__file__).parent / "fixtures" / "smzdm_selected_5060.html").read_text(encoding="utf-8")
    row, _ = watch.extract_smzdm_detail(page, "移动端：铭瑄 MS-RTX5060Ti iCraft OC8G AIGA DLSS 4 显卡")
    assert "2799" not in row["article_content"]
    result = watch.verify_smzdm_offer(row, CFG)
    assert not result[4]
    assert "其他规格" in result[5] and "RTX5060 iCraft" in result[5]


def test_explicit_selected_ti_sku_binds_price_in_next_line():
    page = '<div class="item-name"><article class="txt-detail"><p itemprop="description">该价格商品规格：显存容量：<span>8GB</span>；显卡名称：<b>RTX5060Ti</b><br/>参加活动，最终到手价2899元/件。</p></article></div>'
    row, _ = watch.extract_smzdm_detail(page, TITLE)
    result = watch.verify_smzdm_offer(row, CFG)
    assert result[4] and result[0] == 2899
    assert "spec:" in result[5] and "最终到手价2899" in result[5]


def test_selected_16g_invalidates_8g_body_price():
    row = {"article_title": TITLE, "article_selected_spec": "RTX 5060 Ti 16GB",
           "article_content": "RTX 5060 Ti 8G 券后2899元"}
    result = watch.verify_smzdm_offer(row, CFG)
    assert not result[4] and "其他规格" in result[5]


def test_expired_article_cannot_confirm_a_deal():
    page = '<div class="info J_info">该商品已过期或售罄</div>' + article('<p>RTX 5060 Ti 8G 券后2899元</p>')
    with pytest.raises(ValueError, match="过期"):
        watch.extract_smzdm_detail(page, TITLE)
