from pathlib import Path
import importlib.util


SPEC = importlib.util.spec_from_file_location(
    "watch", Path(__file__).resolve().parents[1] / "watch.py"
)
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)

CFG = {
    "exclude_keywords": ["16G", "16GB", "二手", "拆机", "矿卡"],
    "thresholds": {"normal": 2999, "good_brand": 3099},
    "good_brands": ["微星", "MSI", "华硕", "ASUS", "技嘉", "GIGABYTE"],
}


def test_target_match():
    assert watch.is_target_gpu("微星 RTX 5060 Ti 8G 万图师", CFG)
    assert watch.is_target_gpu("GIGABYTE RTX5060Ti 8GB", CFG)
    assert not watch.is_target_gpu("RTX 5060 8G", CFG)
    assert not watch.is_target_gpu("RTX 5060 Ti 16G", CFG)
    assert not watch.is_target_gpu("二手 RTX 5060 Ti 8G", CFG)


def test_price_parse():
    assert watch.parse_price("券后 ¥2,879") == 2879
    assert watch.parse_price("到手价3099元") == 3099


def test_model_and_discount_numbers_are_not_prices():
    assert watch.parse_price("华硕 RTX 5060 Ti 8GB") is None
    assert watch.parse_price("满3000减300") is None
    assert watch.parse_price("领1000元券") is None


def test_final_price_priority_and_discount_details():
    text = "原价3499元，满3000减300，叠100元券，券后2999元，国补后2899元，最终到手2799元"
    price, page_price, price_type, discount = watch.extract_offer_details(text, 3499)
    assert price == 2799
    assert page_price == 3499
    assert price_type == "最终到手/实付价"
    assert "满3000减300" in discount
    assert "100元券" in discount
    assert "国补" in discount


def test_supported_contextual_price_types():
    examples = [
        ("券后2899元", 2899, "券后价"),
        ("百亿补贴价2888元", 2888, "百亿补贴价"),
        ("满减后2879元", 2879, "满减后价"),
        ("下单价2869元", 2869, "下单价"),
        ("拼单价2859元", 2859, "拼单价"),
        ("PLUS价2849元", 2849, "PLUS价"),
        ("会员价2839元", 2839, "会员价"),
    ]
    for text, expected_price, expected_type in examples:
        price, price_type = watch.extract_final_price(text)
        assert price == expected_price
        assert price_type == expected_type


def test_currency_price_beats_model_number():
    price, page_price, price_type, _ = watch.extract_offer_details(
        "华硕 TX-RTX5060TI-O8G 8GB ￥4968"
    )
    assert price == 4968
    assert page_price == 4968
    assert price_type == "API价"


def test_final_price_is_not_relabelled_as_page_price():
    price, page_price, price_type, _ = watch.extract_offer_details("券后2899元")
    assert price == 2899
    assert page_price is None
    assert price_type == "券后价"


def test_smzdm_api_does_not_fall_back_to_model_number():
    class Response:
        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"rows": self.rows}}

    class Session:
        def __init__(self, rows):
            self.rows = rows

        def get(self, *args, **kwargs):
            return Response(self.rows)

    cfg = dict(CFG)
    cfg.update({"search_keywords": ["RTX 5060 Ti 8G"], "smzdm_fresh_minutes": 180})

    no_price_row = {
        "article_channel_id": "2",
        "article_title": "华硕 RTX 5060 Ti 8GB",
        "article_url": "https://example.com/no-price",
        "article_mall": "京东",
    }
    assert watch.fetch_smzdm_api(Session([no_price_row]), cfg) == []

    deal_row = {
        **no_price_row,
        "article_title": "华硕 RTX 5060 Ti 8GB 国补后2899元",
        "article_url": "https://example.com/deal",
        "article_price": "3499",
    }
    candidates = watch.fetch_smzdm_api(Session([deal_row]), cfg)
    assert len(candidates) == 1
    assert candidates[0].price == 2899
    assert candidates[0].page_price == 3499
    assert candidates[0].price_type == "国补后价"


def test_message_has_price_breakdown():
    candidate = watch.Candidate(
        "什么值得买API",
        "京东",
        "微星 RTX 5060 Ti 8G",
        2899,
        "https://example.com/deal",
        page_price=3299,
        price_type="国补后价",
        discount_info="满3000减300 + 国补",
    )
    message = watch.format_message(candidate, CFG)
    assert "页面/API价：¥3299" in message
    assert "最终到手价：¥2899" in message
    assert "价格类型：国补后价" in message
    assert "优惠条件：满3000减300 + 国补" in message


def test_threshold():
    c1 = watch.Candidate("x", "京东", "微星 RTX 5060 Ti 8G", 3099, "https://x")
    c2 = watch.Candidate("x", "京东", "映众 RTX 5060 Ti 8G", 2999, "https://y")
    assert watch.threshold_for(c1, CFG) == 3099
    assert watch.threshold_for(c2, CFG) == 2999

